import logging
import sys
import os
from pathlib import Path

# usa_tickers.py is inside src, so go up two levels
project_dir = Path(__file__).resolve().parent.parent
log_dir = project_dir / "logging"
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=str(log_dir / "usa_tickers.log"),
    filemode="w" #overwrite the file
                    )
def log_unhandled_exception(exc_type, exc_value, exc_traceback):
    # Keep Ctrl+C behavior unchanged
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    # Save the complete error traceback to the log file
    logging.critical(
        "Unhandled exception — script terminated",
        exc_info=(exc_type, exc_value, exc_traceback),
    )

    # Also display the original traceback in the terminal
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = log_unhandled_exception


from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup
import pandas as pd
from sqlalchemy import create_engine, text,URL 
from dotenv import load_dotenv
load_dotenv()

#### Start scraping data from the stock screener pages and go through the number of pages specified TOTAL_PAGES
url = os.getenv("STOCK_SCREEN_URL")
TOTAL_PAGES = 281

# Configure the web driver and wait object
driver = webdriver.Chrome()
wait = WebDriverWait(driver, 20)
all_data = []
headers = None



try:
    driver.get(url)
    for page_number in range(1 , TOTAL_PAGES + 1):
        wait.until(
            EC.presence_of_element_located(
                (By.ID , "main-table-wrap")
            )
        )

        # Parse the current HTML to extract the table data
        soup = BeautifulSoup(driver.page_source , "html.parser")
        element = soup.find(id="main-table-wrap")

        if element is not None:
            if element.name == "table":
                table = element
            else:
                table = element.find("table")
        else:
            raise ValueError(f"Table container missing on page {page_number}")

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            values = [cell.get_text(strip=True) for cell in cells]
            if values:
                all_data.append(values)
            header = [cell.get_text(strip=True) for cell in row.find_all("th")]
            if headers is None:
                headers = header
        logging.info("Page %s / %s scraped | len(all_data) %s", page_number, TOTAL_PAGES, len(all_data))

        #No need to navigate after the Total_PAGES
        if page_number == TOTAL_PAGES:
            break

        # Capture the current first stock row
        old_first_row = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#main-table-wrap tbody tr")
            )
        )
        old_first_row_text = old_first_row.text

        #Locate and Click Next Button
        next_button = wait.until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[.//span[normalize-space()='Next']]")
                    )
                )
        if (
                    next_button.get_attribute("disabled") is not None
                    or next_button.get_attribute("aria-disabled") == "true"
                ):
                    raise RuntimeError(
                        f"Next button is disabled after page {page_number}. "
                        "The site may have reached its last page."
                    )
        next_button.click()

        # Wait for different stock data to appear
        wait.until(
            lambda d: (
                d.find_element(
                    By.CSS_SELECTOR, "#main-table-wrap tbody tr"
                ).text != old_first_row_text
            )
        )
except TimeoutException:
    logging.error("Timed out waiting for the table or page change.")


finally:
    driver.quit()

# Stop if scraping produced no data
if not all_data or not headers:
    raise RuntimeError("Scraping returned no data or headers.")

# Build DataFrame and add ingestion timestamp
df = pd.DataFrame(all_data, columns=headers)
df["ingestion_date"] = pd.Timestamp.now(tz="UTC")

# PostgreSQL connection
url_object = URL.create(
    drivername=os.getenv("DRIVERNAME"),
    username=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT", "5432")),
    database=os.getenv("DB_NAME")
)

engine = create_engine(url_object)

try:
    # Replace existing records in one transaction
    with engine.begin() as connection:
        connection.execute(
            text("TRUNCATE TABLE raw.ticker_name")
        )

        df.to_sql(
            name="ticker_name",
            con=connection,
            schema="raw",
            if_exists="append",
            index=False
        )

    logging.info("Data loaded into raw.ticker_name.")
    logging.info("Pages requested: %s", TOTAL_PAGES)
    logging.info("Rows collected: %s", len(df))
    logging.info("DataFrame shape: %s", df.shape)
    logging.info("Saved to raw.ticker_name.")

finally:
    engine.dispose()