"""
test_call.py — Make a single test call to verify the pipeline works.
"""
import asyncio
import os
import sys
import logging

from dotenv import load_dotenv
load_dotenv(".env.local")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test-call")

from main import Store, PriceQuote, CallCampaign, export_to_excel, print_summary, save_results
from caller import make_price_enquiry_call

TEST_STORE = Store(
    name="Test Store (Your Phone)",
    phone="+919371637290",
    area="Test Area",
    city="Bangalore",
)

TARGET_PRODUCT = "Samsung 1.5 Ton 5 Star Inverter Split AC (AR18CYNZABE)"


async def main():
    sip_trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
    if not sip_trunk_id:
        logger.error("SIP_OUTBOUND_TRUNK_ID not set in .env.local")
        sys.exit(1)

    logger.info(f"Making test call to {TEST_STORE.phone}...")
    logger.info("Answer the call and pretend to be a shopkeeper!")
    logger.info("The AI agent will ask about prices in Hindi.")

    quote = await make_price_enquiry_call(
        store=TEST_STORE,
        product_description=TARGET_PRODUCT,
        sip_trunk_id=sip_trunk_id,
    )

    campaign = CallCampaign(
        product_description=TARGET_PRODUCT,
        city="Bangalore",
        stores=[TEST_STORE],
        quotes=[quote],
    )

    print_summary(campaign)
    save_results(campaign, "test_results.json")
    export_to_excel(campaign, "test_results.xlsx")

    logger.info(f"Call status: {quote.call_status}")
    logger.info(f"Duration: {quote.call_duration_sec:.0f}s")


if __name__ == "__main__":
    asyncio.run(main())
