## Installation
Tested with python3.10.
```bash
pip install -r requirements.txt
```
```bash
cp .env.example .env
```
And set your `ANCHOR_PROVIDER_URL` in `.env`.

## Run
```bash
streamlit run app.py
```
Then insert the public key of the account you would like to check and adjust asset prices/balances to see new liquidation prices.
