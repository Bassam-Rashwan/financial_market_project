import logging
import sys
import os
from pathlib import Path

# we need to add the logging text in the logging file, so first thing first we need to go to the parent dir
project_dir = Path(__file__).resolve().parent.parent
log_dir = project_dir / "logging"
#in case the logging folder does not exist, so create
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level = logging.INFO,
    format = "%(asctime)s - %(levelname)s - %(message)s",
    filename = str(log_dir / "price_ratios.log"),
    filemode = "w" #overwrite the file
                    )
def log_unhandled_exception(exc_type, exc_value, exc_traceback):

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



import pandas as pd
import time
import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text, URL
from dotenv import load_dotenv
load_dotenv()

# Connect to Database to get the Symbols
url_object = URL.create(
    drivername=os.getenv("DRIVERNAME"),
    username=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT", "5432")),
    database=os.getenv("DB_NAME")
)
engine = create_engine(url_object)
query = text('SELECT * FROM "raw"."ticker_name"')
ticker_name = pd.read_sql(query , engine)

# Scraping the Price Ratios data for each ticker
all_results = []
failed_symbols = []

for symbol in ticker_name["Symbol"]:
    if not symbol:
        continue
    # Construct the URL for the price ratio page
    url = os.getenv("RATIOS_BASE_URL") + symbol.lower() + os.getenv("RATIOS_PATH")
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an exception for HTTP errors
         
        if response.status_code == 429:
            logging.warning("%s : Too many requests", symbol)
            retry_after = int(response.headers.get("Retry-After", 60))
            logging.info("%s : Retrying after %s seconds", symbol, retry_after)
            time.sleep(retry_after)
            continue

        soup = BeautifulSoup(response.content , "html.parser")
        element = soup.find(id = "main-table-wrap-price-ratios")
        table = None

        if element is not None:
            if element.name == "table":
                table = element
            else:
                table = element.find("table")  # Find the table within the wrapper if it exists
        if table is None:
            raise ValueError("No table found on the page")

        #store the rows in list
        data = []
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            values = [
                        cell.get_text(" ", strip=True)
                        for cell in cells
                    ]
            if values:
                data.append(values)
        df = pd.DataFrame(data)
        # Your original layout assumption:
        # row 0 = extra header; row 1 = column names.
        df.columns = df.iloc[1]  # Set the column names from the second row
        df = df.iloc[2:].copy()
        # Remove the first two words from each period header.
        df.columns = [
                    " ".join(str(column).split()[2:])
                    for column in df.columns
                ]
        #Rename the first column by position
        df.columns = ["Metric_Name" , *df.columns[1:]]

        #Unpivot the table
        melted = df.melt(
            id_vars = "Metric_Name",
            var_name = "Period",
            value_name = "Value"
        )

        #Convert the period to date type
        melted["Period"] = pd.to_datetime(melted["Period"], 
                                          format = "%b %d, %Y",
                                          errors="coerce")
        # Stop rather than silently discard unexpected period labels.
        if melted["Period"].isna().any():
            raise ValueError(
                "Some period headers could not be parsed. "
                f"Headers after cleanup: {df.columns.tolist()}"
            )
        melted["Value"] = pd.to_numeric(
                    melted["Value"].str.replace(",", "", regex=False),
                    errors="coerce",
                )
        melted["Symbol"] = symbol
        all_results.append(melted)
        logging.info("%s : Collected %s rows",symbol, len(melted))

    except (requests.RequestException, ValueError) as exc:
        failed_symbols.append({
            "Symbol": symbol,
            "Error": str(exc),
            })
        logging.error("%s : Failed with error %s", symbol, str(exc))
#Combine Results from every successful symbol
final_df = (
    pd.concat(all_results, ignore_index=True)
        )
#Add ingestion date to the final dataframe
final_df["ingestion_date"] = pd.Timestamp.now()
errors_df = pd.DataFrame(
    failed_symbols,
    columns=["Symbol", "Error"],
                    )

# ingesting the final dataframe into the database
#First Truncate the table
# Database ingestion
start_time = time.perf_counter()

logging.info(
    "Starting ingestion of %s rows",
    len(final_df)
)

with engine.begin() as conn:
    conn.execute(text("TRUNCATE TABLE raw.price_ratios"))

    final_df.to_sql(
        name="price_ratios",
        con=conn,
        schema="raw",
        if_exists="append",
        index=False,
        chunksize=5000
    )

elapsed = time.perf_counter() - start_time

logging.info(
    "Ingested %s rows into raw.price_ratios in %.2f seconds",
    len(final_df),
    elapsed
)

engine.dispose()

logging.info("Ingested %s rows into raw.price_ratios", len(final_df))
logging.info("Saved to raw.price_ratios")
logging.info("Failed symbols: %s", len(errors_df))
