import streamlit as st
from breeze_connect import BreezeConnect
import urllib.parse

# --- 1. SETUP PAGE ---
st.set_page_config(page_title="Breeze Login", page_icon="🔐")
st.title("🔐 ICICI Breeze Login System")

# --- 2. GET API KEYS (From Secrets) ---
try:
    API_KEY = st.secrets["API_KEY"]
    API_SECRET = st.secrets["API_SECRET"]
except:
    st.error("❌ API Keys are missing in Streamlit Secrets.")
    st.stop()

# --- 3. MAINTAIN SESSION (The Brain) ---
if 'breeze' not in st.session_state:
    st.session_state.breeze = None
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False

# --- 4. LOGIN LOGIC ---
if not st.session_state.logged_in:
    st.warning("🔴 You are currently OFFLINE")

    # A. Generate the Link for you
    login_url = f"https://api.icicidirect.com/apiuser/login?api_key={urllib.parse.quote_plus(API_KEY)}"
    st.markdown(f"👉 [**Click here to get your Session Token**]({login_url})")

    # B. Input Box for Token
    token = st.text_input("Paste the Session Token here:", type="password")

    # C. Connect Button
    if st.button("Connect Now"):
        if token:
            try:
                # The Actual Connection Code
                client = BreezeConnect(api_key=API_KEY)
                client.generate_session(api_secret=API_SECRET, session_token=token)
                
                # Save to "Brain"
                st.session_state.breeze = client
                st.session_state.logged_in = True
                st.rerun() # Refresh the page
            except Exception as e:
                st.error(f"Login Failed: {e}")
        else:
            st.warning("Please paste the token first.")

# --- 5. SUCCESS SCREEN ---
else:
    st.success("🟢 LOGIN SUCCESSFUL!")
    st.write("You are connected to ICICI Direct.")
    
    # Simple Logout Button
    if st.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.breeze = None
        st.rerun()
      
