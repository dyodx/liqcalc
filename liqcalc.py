import copy
import decimal
import pandas as pd
import streamlit as st
from solders.pubkey import Pubkey
from driftpy.drift_client import DriftClient
from driftpy.drift_user import DriftUser
from driftpy.account_subscription_config import AccountSubscriptionConfig
from driftpy.oracles.oracle_id import get_oracle_id
from driftpy.addresses import (
    get_user_stats_account_public_key,
    get_user_account_public_key,
)
from driftpy.types import SpotPosition


def get_market_name(market):
    return bytes(market.name).decode("utf-8").strip("\x00")


async def liqcalc(clearing_house: DriftClient):
    st.title("Liquidation Calculator")
    # Get authority address
    authority_address = st.text_input(
        "Authority address", help="Enter authority address to analyze positions"
    )
    if authority_address:
        if len(authority_address) < 5:
            st.warning("Please enter a valid authority address")
            return
    else:
        return

    if (
        "current_authority" not in st.session_state
        or st.session_state.current_authority != authority_address
    ):
        st.session_state.current_authority = authority_address
        st.session_state.pop("clearing_house_cache", None)
        st.session_state.pop("user_and_slot", None)
        st.session_state.pop("subaccount", None)
    authority_pubkey = Pubkey.from_string(str(authority_address).strip())

    # Initialize market data
    if "clearing_house_cache" not in st.session_state:
        with st.spinner("Initializing market data..."):
            await clearing_house.account_subscriber.update_cache()
            st.session_state.clearing_house_cache = (
                clearing_house.account_subscriber.cache
            )

    # Create working copy of clearing house cache containing data from initial RPC calls
    clearing_house.account_subscriber.cache = copy.deepcopy(
        st.session_state.clearing_house_cache
    )

    # Subaccount selection
    user_stats_pubkey = get_user_stats_account_public_key(
        clearing_house.program_id, authority_pubkey
    )
    try:
        user_stats = await clearing_house.program.account["UserStats"].fetch(
            user_stats_pubkey
        )
    except:
        st.error("Error fetching account. Please check the authority address.")
        return
    subaccount_options = list(range(user_stats.number_of_sub_accounts_created))
    selected_subaccount = st.selectbox(
        "Select subaccount",
        subaccount_options,
        format_func=lambda x: f"Subaccount {x}",
        help="Select which subaccount to analyze",
    )
    user_account_pubkey = get_user_account_public_key(
        clearing_house.program_id, authority_pubkey, selected_subaccount
    )

    # Initialize user
    user = DriftUser(
        clearing_house,
        user_public_key=user_account_pubkey,
        account_subscription=AccountSubscriptionConfig("cached"),
    )
    if (
        "subaccount" not in st.session_state
        or st.session_state.subaccount != selected_subaccount
    ):
        with st.spinner("Initializing user data..."):
            await user.account_subscriber.update_cache()
            st.session_state.subaccount = selected_subaccount
            st.session_state.user_and_slot = user.account_subscriber.user_and_slot
            for key in list(st.session_state.keys()):
                if key.startswith("price_") or key.startswith("balance_"):
                    del st.session_state[key]
            if not st.session_state.user_and_slot:
                st.info("No data found for this subaccount")
                return

    # Create working copy of user cache containing data from initial RPC calls
    user.account_subscriber.user_and_slot = copy.deepcopy(
        st.session_state.user_and_slot
    )

    spot_positions = user.get_active_spot_positions()
    perp_positions = user.get_active_perp_positions()

    if not spot_positions and not perp_positions:
        st.info("No active positions found for this account")
        return

    # Map markets to oracles
    oracle_markets = {}
    for pos in perp_positions:
        market = user.get_perp_market_account(pos.market_index)
        oracle_key = get_oracle_id(market.amm.oracle, market.amm.oracle_source)
        if oracle_key not in oracle_markets:
            oracle_markets[oracle_key] = []
        oracle_markets[oracle_key].append(
            {
                "type": "perp",
                "market": market,
                "name": get_market_name(market),
                "index": pos.market_index,
            }
        )

    max_spot_decimals = 0
    for pos in spot_positions:
        market = user.get_spot_market_account(pos.market_index)
        oracle_key = get_oracle_id(market.oracle, market.oracle_source)
        if oracle_key not in oracle_markets:
            oracle_markets[oracle_key] = []
        oracle_markets[oracle_key].append(
            {
                "type": "spot",
                "market": market,
                "name": get_market_name(market),
                "index": pos.market_index,
            }
        )
        max_spot_decimals = max(max_spot_decimals, market.decimals)

    # Price adjustment controls
    st.radio(
        "Adjustment Mode",
        ["Value", "Percentage"],
        horizontal=True,
        key="adjustment_mode",
    )
    st.subheader(
        "Price Adjustments",
        help="Adjust prices for selected markets to see effect on positions",
    )

    price_changes = {}
    price_cols = st.columns(3)
    for i, (oracle_key, markets) in enumerate(oracle_markets.items()):
        market_names = [m["name"] for m in markets]
        market_str = " / ".join(market_names)

        market = markets[0]["market"]
        if markets[0]["type"] == "perp":
            oracle_data = user.get_oracle_data_for_perp_market(markets[0]["index"])
        else:
            oracle_data = user.get_oracle_data_for_spot_market(markets[0]["index"])
        initial_price = float(oracle_data.price) / 1e6

        value_key = f"price_value_{oracle_key}"
        pct_key = f"price_pct_change_{oracle_key}"
        col = price_cols[i % 3]
        if st.session_state.adjustment_mode == "Value":
            if value_key not in st.session_state:
                if pct_key in st.session_state:
                    st.session_state[value_key] = initial_price * (
                        1 + st.session_state[pct_key] / 100
                    )
                else:
                    st.session_state[value_key] = initial_price

            new_price = col.number_input(
                f"{market_str} price ($)",
                step=0.01,
                key=value_key,
            )
            price_changes[oracle_key] = {"price": new_price}

        else:
            # Percentage mode
            if pct_key not in st.session_state:
                if value_key in st.session_state:
                    st.session_state[pct_key] = (
                        (st.session_state[value_key] - initial_price) / initial_price
                    ) * 100
                else:
                    st.session_state[pct_key] = 0.0

            pct_change = col.number_input(
                f"{market_str} price change (%)",
                min_value=-100.0,
                step=1.0,
                key=pct_key,
            )

            new_price = initial_price * (1 + pct_change / 100)
            price_changes[oracle_key] = {"price": new_price}

    # Collateral adjustment controls
    st.subheader(
        "Collateral Adjustments",
        help="Adjust spot token balances to see effect on positions",
    )

    collateral_changes = {}
    collateral_cols = st.columns(3)
    for i, pos in enumerate(spot_positions):
        market = user.get_spot_market_account(pos.market_index)
        tokens = user.get_token_amount(pos.market_index)
        initial_ui_tokens = tokens / (10**market.decimals)
        if "original_token_amounts" not in st.session_state:
            st.session_state.original_token_amounts = {}

        # Set amount of tokens to RPC amounts
        st.session_state.original_token_amounts[pos.market_index] = tokens

        value_key = f"balance_value_{pos.market_index}"
        pct_key = f"balance_pct_change_{pos.market_index}"
        col = collateral_cols[i % 3]
        if st.session_state.adjustment_mode == "Value":
            if value_key not in st.session_state:
                if pct_key in st.session_state:
                    st.session_state[value_key] = initial_ui_tokens * (
                        1 + st.session_state[pct_key] / 100
                    )
                else:
                    st.session_state[value_key] = initial_ui_tokens

            new_balance = col.number_input(
                f"{get_market_name(market)} balance",
                step=1.0,
                key=value_key,
                format=f"%.{market.decimals}f",
            )

            collateral_changes[pos.market_index] = {
                "balance": new_balance,
                "decimals": market.decimals,
            }
        else:
            # Percentage mode
            if pct_key not in st.session_state:
                if value_key in st.session_state:
                    st.session_state[pct_key] = (
                        (st.session_state[value_key] - initial_ui_tokens)
                        / initial_ui_tokens
                    ) * 100
                else:
                    st.session_state[pct_key] = 0.0

            pct_change = col.number_input(
                f"{get_market_name(market)} balance change (%)",
                min_value=-100.0,
                step=1.0,
                key=pct_key,
            )

            new_balance = initial_ui_tokens * (1 + pct_change / 100)
            collateral_changes[pos.market_index] = {
                "balance": new_balance,
                "decimals": market.decimals,
            }

    # Apply price updates
    if price_changes:
        cache = copy.deepcopy(clearing_house.account_subscriber.cache)
        oracle_price_data = cache["oracle_price_data"]

        for oracle_key, change_info in price_changes.items():
            if oracle_key in oracle_price_data:
                oracle_data = oracle_price_data[oracle_key]
                if oracle_data and hasattr(oracle_data, "data"):
                    oracle_data.data.price = int(change_info["price"] * 1e6)

        # Update cache
        cache["oracle_price_data"] = oracle_price_data
        clearing_house.account_subscriber.cache = cache
        user.drift_client = clearing_house

    # Apply collateral updates
    if collateral_changes:
        new_positions = []
        # Loop through the RPC user positions
        for pos in user.get_user_account().spot_positions:
            if pos.market_index in collateral_changes:
                change_info = collateral_changes[pos.market_index]
                # Get the RPC amount of tokens
                original_tokens = st.session_state.original_token_amounts[
                    pos.market_index
                ]
                # Get the amount of tokens the user has last input
                new_tokens = int(
                    change_info["balance"] * (10 ** change_info["decimals"])
                )
                # Scale the underlying scaled_balance by the ratio of new to original tokens
                new_scaled_balance = (
                    int(pos.scaled_balance * (new_tokens / original_tokens))
                    if original_tokens != 0
                    else 0
                )

                # Create new position with updated balance
                new_position = SpotPosition(
                    scaled_balance=new_scaled_balance,
                    open_bids=pos.open_bids,
                    open_asks=pos.open_asks,
                    cumulative_deposits=pos.cumulative_deposits,
                    market_index=pos.market_index,
                    balance_type=pos.balance_type,
                    open_orders=pos.open_orders,
                    padding=pos.padding,
                )
                new_positions.append(new_position)
            else:
                new_positions.append(pos)

        # Update user's spot positions
        user.account_subscriber.user_and_slot.data.spot_positions = new_positions

    # Calculate liquidation prices and show active positions
    st.subheader(
        "Active Positions", help="Position analysis given the above price settings"
    )
    spot_data = []
    perp_data = []

    # Process spot positions
    for pos in spot_positions:
        market = user.get_spot_market_account(pos.market_index)
        tokens = user.get_token_amount(pos.market_index)

        oracle_price_data = user.get_oracle_data_for_spot_market(pos.market_index)

        if oracle_price_data:
            oracle_price = float(oracle_price_data.price) / 1e6
            balance = tokens / (10**market.decimals)

            spot_data.append(
                {
                    "Name": get_market_name(market),
                    "Balance": balance,
                    "Net value ($)": balance * oracle_price,
                    "Price ($)": oracle_price,
                    "Liquidation price ($)": float(
                        user.get_spot_liq_price(pos.market_index)
                    )
                    / 1e6,
                }
            )

    # Process perp positions
    for pos in perp_positions:
        market = user.get_perp_market_account(pos.market_index)
        oracle_price_data = user.get_oracle_data_for_perp_market(pos.market_index)

        if oracle_price_data:
            oracle_price = float(oracle_price_data.price) / 1e6
            base_size = pos.base_asset_amount / 1e9

            perp_data.append(
                {
                    "Name": get_market_name(market),
                    "Base size": base_size,
                    "Notional ($)": base_size * oracle_price,
                    "Price ($)": oracle_price,
                    "Liquidation price ($)": float(
                        user.get_perp_liq_price(pos.market_index)
                    )
                    / 1e6,
                }
            )

    # Display results
    result_cols = st.columns([1, 1])

    with result_cols[0]:
        if spot_data:
            st.markdown("#### Spot Positions")
            spot_df = pd.DataFrame(spot_data)
            spot_df["Net value ($)"] = spot_df["Net value ($)"].round(2)
            spot_df["Price ($)"] = spot_df["Price ($)"].round(2)
            spot_df["Liquidation price ($)"] = spot_df["Liquidation price ($)"].round(2)
            st.dataframe(
                spot_df,
                column_config={
                    "Balance": st.column_config.NumberColumn(
                        format=f"%.{max_spot_decimals}f"
                    ),
                    "Net value ($)": st.column_config.NumberColumn(format="%.2f"),
                    "Price ($)": st.column_config.NumberColumn(format="%.2f"),
                    "Liquidation price ($)": st.column_config.NumberColumn(
                        format="%.2f"
                    ),
                },
                use_container_width=True,
            )
        else:
            st.info("No spot positions to display")

    with result_cols[1]:
        if perp_data:
            st.markdown("#### Perp Positions")
            perp_df = pd.DataFrame(perp_data)
            perp_df["Notional ($)"] = perp_df["Notional ($)"].round(2)
            perp_df["Price ($)"] = perp_df["Price ($)"].round(2)
            perp_df["Liquidation price ($)"] = perp_df["Liquidation price ($)"].round(2)
            st.dataframe(
                perp_df,
                column_config={
                    "Base size": st.column_config.NumberColumn(format=f"%.9f"),
                    "Notional ($)": st.column_config.NumberColumn(format="%.2f"),
                    "Price ($)": st.column_config.NumberColumn(format="%.2f"),
                    "Liquidation price ($)": st.column_config.NumberColumn(
                        format="%.2f"
                    ),
                },
                use_container_width=True,
            )
        else:
            st.info("No perp positions to display")

    # Show account health
    health = user.get_health()
    st.metric(
        "Account Health",
        f"{health}%",
    )
