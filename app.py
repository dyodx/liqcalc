import os
import asyncio
import traceback
import streamlit as st
from dotenv import load_dotenv
from anchorpy import Provider, Wallet
from driftpy.drift_client import DriftClient, AccountSubscriptionConfig
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from liqcalc import liqcalc

load_dotenv()

def main():
    st.set_page_config(
        page_title="Drift Liquidation Calculator",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    rpc_url = os.getenv("ANCHOR_PROVIDER_URL")
    if not rpc_url:
        st.error("Missing ANCHOR_PROVIDER_URL environment variable")
        return

    try:
        # Initialize client
        wallet = Wallet(Keypair())
        connection = AsyncClient(rpc_url)
        provider = Provider(connection, wallet)
        
        clearing_house = DriftClient(
            provider.connection,
            provider.wallet,
            "mainnet",
            account_subscription=AccountSubscriptionConfig("cached"),
        )

        # Run
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(liqcalc(clearing_house))
        
    except Exception as e:
        st.error(f"App error: {str(e)}")
        st.code(traceback.format_exc())

if __name__ == "__main__":
    main()
