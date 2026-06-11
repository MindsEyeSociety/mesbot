#!/usr/bin/python3

import time
import requests
import mysql.connector
from mysql.connector import errorcode
import os
from dotenv import load_dotenv
from urllib.parse import urlencode, urlparse, parse_qs
from flask import Flask, request, redirect
from datetime import datetime

load_dotenv()

app = Flask(__name__)


def get_cursor():
    """Return a DB cursor, reconnecting the global connection if it went stale.

    Tries ping/reconnect first; falls back to a fresh connection. Raises
    mysql.connector.Error if the DB cannot be reached after all attempts.
    """
    global cnx
    try:
        cnx.ping(reconnect=True, attempts=3, delay=2)
    except mysql.connector.Error:
        cnx = mysql.connector.connect(
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            host=os.getenv('DB_SERVER'),
            database=os.getenv('DB_DATABASE')
        )
    return cnx.cursor()


def is_membership_expired(membership_expiration: str) -> bool:
    expiration_date = datetime.strptime(membership_expiration, "%Y-%m-%d")
    current_date = datetime.today()
    return expiration_date < current_date

@app.route("/oauth/callback")
def oauth_callback():
    state = request.args.get("state")
    code = request.args.get("code")
    if not code:
        error = request.args.get("error")
        if error:
            return f"Error: {error}", 400
        return "Error: No code provided in the URL.", 400

    # Exchange auth code for access token
    CLIENT_ID=os.getenv('CLIENT_ID')
    CLIENT_SECRET=os.getenv('CLIENT_SECRET')
    REDIRECT_URI=os.getenv('REDIRECT_URI')
    TOKEN_URL=os.getenv('TOKEN_URL')
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': code,
        'redirect_uri': REDIRECT_URI,
        'grant_type': 'authorization_code'
    }

    response = requests.post(TOKEN_URL, data=data)

    if response.status_code != 200:
        print(f"Error fetching token {response.text}")
        return f"Error fetching token: {response.text}", 500

    token_data = response.json()
    access_token = token_data.get('access_token')

    if not access_token:
        print(f"Error: No access token received")
        return "Error: No access token received", 500

    # Use the access token to get user info

    user_info_response = requests.get(os.getenv('USER_API_URL'), headers={'Authorization': f'Bearer {access_token}'})

    if user_info_response.status_code !=200:
        print(f"Error fetching user info: {user_info_response.text}")
        return f"Error fetching user info: {user_info_response.text}", 500

    user_info = user_info_response.json()
    print(state)
    print(user_info)
    if user_info['membershipType'] in {"Full","Trial"}:
        print(f"Valid membership")
    else:
        print(f"Invalid membership, you must have a Trial or Full membership")
        return f"Invalid membership, you must have a Trial or Full membership", 400
    if(is_membership_expired(user_info['membershipExpiration'])):
        print(f"Membership expired {user_info['membershipNumber']} {user_info['membershipExpiration']}")
        return f"Membership expired. Please renew your membership and try again.", 400
    MAX_RETRIES = 5
    DELAY_SECONDS = 0.2
    try:
        cursor=get_cursor()
    except mysql.connector.Error as e:
        print(f"DB unavailable in oauth_callback: {e}")
        return "Service temporarily unavailable", 503
    cursor.execute("SELECT user_id FROM user_states")
    print(f"{cursor.fetchall()}")
    for attempt in range(MAX_RETRIES):
        cursor.execute("SELECT user_id FROM user_states WHERE state = %s", (state,))
        print(f"SELECT user_id FROM user_states WHERE state = '{state}'")
        result = cursor.fetchone()
        cursor.fetchall()
        print(result)
        if result: #if we find it, then stop here
            break
        if attempt < MAX_RETRIES - 1:
            time.sleep(DELAY_SECONDS)
    if not result:
        return "Error: expired link. Request a new link from MESBot with the !auth command. Link is only valid for 5 minutes for security purposes.", 400
    user_id = result[0]
    cursor.execute(
            "INSERT INTO user_authorizations (discord_user_id, last_authorized, access_token) VALUES (%s, CURRENT_TIMESTAMP, %s) "
            "ON DUPLICATE KEY UPDATE last_authorized = CURRENT_TIMESTAMP",
            (user_id, user_info['membershipNumber'])
    )
    cnx.commit()
    cursor.close()
    return f"Success", 200


@app.route("/oauth/authorize")
def oauth_authorize():
    #pull out state from the discord bot so we can match up the user at the end
    state = request.args.get("state")
    #redirect user to the oauth2 url
    AUTHORIZE_URL=os.getenv('AUTHORIZE_URL')
    CLIENT_ID=os.getenv('CLIENT_ID')
    REDIRECT_URI=os.getenv('REDIRECT_URI')
    auth_url = f"{AUTHORIZE_URL}?{urlencode({'client_id': CLIENT_ID, 'redirect_uri': REDIRECT_URI, 'response_type': 'code', 'state': state})}"
    print(auth_url)
    return redirect(auth_url)



try:
  cnx = mysql.connector.connect(user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'), host= os.getenv('DB_SERVER'),
                                database=os.getenv('DB_DATABASE'))
except mysql.connector.Error as err:
  if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
    print("Something is wrong with your user name or password")
  elif err.errno == errorcode.ER_BAD_DB_ERROR:
    print("Database does not exist")
  else:
    print(err)

#cursor=cnx.cursor()

if __name__ == "__main__":
    app.run(host="::", port=5000)
