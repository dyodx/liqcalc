import copy
import traceback
import pandas as pd
import streamlit as st
from solders.pubkey import Pubkey
from driftpy.drift_client import DriftClient
from driftpy.drift_user import DriftUser
from driftpy.account_subscription_config import AccountSubscriptionConfig
from driftpy.oracles.oracle_id import get_oracle_id

def get_market_name(market):
    return bytes(market.name).decode('utf-8').strip('\x00')

async def liqcalc(clearing_house: DriftClient):
    st.title("Liquidation Calculator")
    
    user_pk = st.text_input(
        "User Account",
        help="Enter the authority address to analyze positions"
    )

    if not user_pk or len(user_pk) < 44:
        st.warning("Please enter a valid user account address")
        return

    try:
        user_pubkey = Pubkey.from_string(str(user_pk).strip())
        
        # Initialize market data
        if 'clearing_house_cache' not in st.session_state:
            with st.spinner("Initializing market data..."):
                await clearing_house.account_subscriber.update_cache()
                st.session_state.clearing_house_cache = clearing_house.account_subscriber.cache
            st.success("Market data updated!")

        # Create working copy of cache containing data from initial RPC calls
        clearing_house.account_subscriber.cache = copy.deepcopy(st.session_state.clearing_house_cache)
        
        # Initialize user 
        user = DriftUser(
            clearing_house,
            user_public_key=user_pubkey,
            account_subscription=AccountSubscriptionConfig("cached"),
        )
        await user.account_subscriber.update_cache()

        spot_positions = user.get_active_spot_positions()
        perp_positions = user.get_active_perp_positions()

        if not spot_positions and not perp_positions:
            st.info("No active positions found for this account")
            return

        # Map markets to oracles
        oracle_markets = {}
        for pos in perp_positions:
            market = user.get_perp_market_account(pos.market_index)
            oracle_key = get_oracle_id(
                market.amm.oracle,
                market.amm.oracle_source
            )
            if oracle_key not in oracle_markets:
                oracle_markets[oracle_key] = []
            oracle_markets[oracle_key].append({
                'type': 'perp',
                'market': market,
                'name': get_market_name(market),
                'index': pos.market_index
            })
        
        for pos in spot_positions:
            market = user.get_spot_market_account(pos.market_index)
            oracle_key = get_oracle_id(
                market.oracle,
                market.oracle_source
            )
            if oracle_key not in oracle_markets:
                oracle_markets[oracle_key] = []
            oracle_markets[oracle_key].append({
                'type': 'spot',
                'market': market,
                'name': get_market_name(market),
                'index': pos.market_index
            })

        # Price adjustment controls
        st.subheader("Price Adjustments", help="Adjust prices for selected markets to see effect on positions")
        price_cols = st.columns(3)
        price_changes = {}
        
        for i, (oracle_key, markets) in enumerate(oracle_markets.items()):
            market_names = [m['name'] for m in markets]
            market_str = " / ".join(market_names)
            
            price_delta = price_cols[i % 3].number_input(
                f"{market_str} Price Change (%)",
                min_value=-100.0,
                max_value=1000.0,
                value=0.0,
                step=1.0,
                key=f"price_delta_{i}"
            )
            
            if price_delta != 0:
                price_changes[oracle_key] = {
                    'delta': price_delta,
                    'markets': markets
                }

        # Apply price updates
        if price_changes:
            cache = copy.deepcopy(clearing_house.account_subscriber.cache)
            oracle_price_data = cache['oracle_price_data']
            
            for oracle_key, change_info in price_changes.items():
                if oracle_key in oracle_price_data:
                    oracle_data = oracle_price_data[oracle_key]
                    if oracle_data and hasattr(oracle_data, 'data'):
                        current_price = oracle_data.data.price
                        new_price = int(current_price * (1 + change_info['delta']/100))
                        oracle_data.data.price = new_price
            
            # Update cache
            cache["oracle_price_data"] = oracle_price_data
            clearing_house.account_subscriber.cache = cache
            user.drift_client = clearing_house

        # Calculate liquidation prices and show active positions
        st.subheader("Active Positions", help="Position analysis given the above price settings")
        spot_data = []
        perp_data = []

        # Process spot positions
        for pos in spot_positions:
            market = user.get_spot_market_account(pos.market_index)
            tokens = user.get_token_amount(pos.market_index)
            oracle_price_data = user.get_oracle_data_for_spot_market(pos.market_index)
            
            if oracle_price_data:
                oracle_price = float(oracle_price_data.price) / 1e6
                liq_price = float(user.get_spot_liq_price(pos.market_index)) / 1e6
                
                spot_data.append({
                    "name": get_market_name(market),
                    "balance": tokens / (10 ** market.decimals),
                    "current_price ($)": oracle_price,
                    "liquidation_price ($)": liq_price,
                    "net_value ($)": tokens * oracle_price / (10 ** market.decimals)
                })

        # Process perp positions  
        for pos in perp_positions:
            market = user.get_perp_market_account(pos.market_index)
            oracle_price_data = user.get_oracle_data_for_perp_market(pos.market_index)
            
            if oracle_price_data:
                oracle_price = float(oracle_price_data.price) / 1e6
                liq_price = float(user.get_perp_liq_price(pos.market_index)) / 1e6

                perp_data.append({
                    "name": get_market_name(market),
                    "base_size": pos.base_asset_amount / 1e9,
                    "current_price ($)": oracle_price,
                    "liquidation_price ($)": liq_price,
                    "notional ($)": (pos.base_asset_amount / 1e9) * oracle_price
                })

        # Display results
        result_cols = st.columns([1, 1])
        
        with result_cols[0]:
            if spot_data:
                st.markdown("#### Spot Positions")
                spot_df = pd.DataFrame(spot_data)
                spot_df['current_price ($)'] = spot_df['current_price ($)'].round(2)
                spot_df['liquidation_price ($)'] = spot_df['liquidation_price ($)'].round(2)
                spot_df['net_value ($)'] = spot_df['net_value ($)'].round(2)
                st.dataframe(spot_df, use_container_width=True)
            else:
                st.info("No spot positions to display")

        with result_cols[1]:
            if perp_data:
                st.markdown("#### Perp Positions")
                perp_df = pd.DataFrame(perp_data)
                perp_df['current_price ($)'] = perp_df['current_price ($)'].round(2)
                perp_df['liquidation_price ($)'] = perp_df['liquidation_price ($)'].round(2)
                perp_df['notional ($)'] = perp_df['notional ($)'].round(2)
                st.dataframe(perp_df, use_container_width=True)
            else:
                st.info("No perp positions to display")

        # Show account health
        health = user.get_health()
        st.metric(
            "Account Health",
            f"{health:.2f}%",
        )
                
    except Exception as e:
        st.error(f"Error processing user account: {str(e)}")
        st.code(traceback.format_exc())
