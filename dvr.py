import streamlit as st
from breeze_connect import BreezeConnect
import requests
import datetime
import pytz
import urllib.parse
import os

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Breeze Auto-Bot", page_icon="🤖")

# --- 1. SETUP & CONSTANTS ---
IST = pytz.timezone('Asia/Kolkata')

# ⚠️ SECURITY: Fetch keys from Streamlit Secrets
try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
    TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
    TELEGRAM_GROUP_ID = st.secrets["TELEGRAM_GROUP_ID"]
except Exception as e:
    st.error(f"❌ Missing Secrets: {e}")
    st.stop()

# --- 2. HELPER FUNCTIONS ---
def send_telegram_message(msg):
    """Sends message to your Telegram Group"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_GROUP_ID,
            'text': msg,
            'parse_mode': 'HTML'
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        st.error(f"Telegram Error: {e}")

# --- 3. SESSION STATE INITIALIZATION ---
if 'breeze' not in st.session_state:
    st.session_state.breeze = None
if 'is_logged_in' not in st.session_state:
    st.session_state.is_logged_in = False

# --- 4. SIDEBAR (Login System) ---
with st.sidebar:
    st.header("🔐 Authentication")
    
    if st.session_state.is_logged_in:
        st.success(f"Online since: {datetime.datetime.now(IST).strftime('%H:%M')}")
        if st.button("Logout"):
            st.session_state.is_logged_in = False
            st.session_state.breeze = None
            st.rerun()
    else:
        st.warning("🔴 System Offline")
        
        # Login Link Generation
        login_url = f"https://api.icicidirect.com/apiuser/login?api_key={urllib.parse.quote_plus(API_KEY)}"
        st.markdown(f"👉 [**Click Here to Generate Token**]({login_url})")
        
        # Input for Session Token
        session_token_input = st.text_input("Paste Session Token:", type="password")
        
        if st.button("Start System"):
            if session_token_input:
                try:
                    # Initialize Breeze
                    breeze = BreezeConnect(api_key=API_KEY)
                    breeze.generate_session(api_secret=API_SECRET, session_token=session_token_input)
                    
                    # Test Connection
                    breeze.get_quotes(stock_code="NIFTY", exchange_code="NSE", product_type="cash")
                    
                    # Save to Session State
                    st.session_state.breeze = breeze
                    st.session_state.is_logged_in = True
                    
                    # Send Telegram Alert
                    send_telegram_message(f"✅ <b>System Online</b>\n{datetime.datetime.now(IST):%d-%b-%Y %H:%M IST}")
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Login Failed: {e}")
            else:
                st.warning("Please enter the token.")

# --- 5. MAIN DASHBOARD ---
st.title("🤖 DVR Nifty 50 Bot")

if st.session_state.is_logged_in:
    st.write("---")
    st.info("System is running. You can add your Strategy Code here.")
    
    # EXAMPLE: Fetch Nifty Data
    if st.button("Test Telegram Alert"):
        send_telegram_message("🔔 This is a test alert from your Streamlit Bot!")
        st.success("Test Message Sent!")
            
else:
    st.info("👈 Please Login from the Sidebar to start.")
  
