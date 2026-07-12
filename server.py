#!/usr/bin/python3

import time
import requests
import mysql.connector
from mysql.connector import errorcode
import os
from dotenv import load_dotenv
from urllib.parse import urlencode, urlparse, parse_qs
from flask import Flask, request, redirect
from datetime import datetime, timezone, timedelta
import logging

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


VALID_MEMBERSHIP_TYPES = frozenset({"Full", "Trial", "Monthly"})
"""Portal ``membershipType`` values that entitle a member to Discord access.

Mirrors the types the portal actually issues to paying//trialling members. The portal also
carries ``None`` (registered, never bought a membership), ``DNR``, ``Expelled``, ``Resigned``
and legacy junk (``0``, NULL) — none of which may verify. ``Monthly`` is a real, current
subscription tier and belongs here: omitting it silently locked active subscribers out of
Discord with a message telling them to get a membership they already had.

Membership *validity* is a separate check: :func:`is_membership_expired` still gates on the
expiration date, so a lapsed Monthly/Full/Trial member is rejected on that basis instead.
"""


def db_connect():
    """Open a MySQL connection for the OAuth server, with autocommit ON.

    ``autocommit=True`` is load-bearing, not a style choice. mysql-connector defaults it to
    False, and the server runs REPEATABLE READ, so a connection's first SELECT pins a consistent
    snapshot that survives until the transaction ends — making rows committed by the *other*
    process (main.py's loops) invisible to this one. Autocommit ends each statement's
    transaction, so every query reads fresh data. The explicit ``cnx.commit()`` after the
    ``user_authorizations`` write remains correct (it becomes a no-op).

    @return An open ``mysql.connector`` connection to the bot database.
    @throws mysql.connector.Error if the database cannot be reached.
    @see main.py's ``db_connect`` — the same fix on the bot side of the same database.
    """
    return mysql.connector.connect(
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        host=os.getenv('DB_SERVER'),
        database=os.getenv('DB_DATABASE'),
        autocommit=True,
    )


def get_cursor():
    """Return a DB cursor, reconnecting the global connection if it went stale.

    Tries ping/reconnect first; falls back to a fresh connection. Raises
    mysql.connector.Error if the DB cannot be reached after all attempts.
    """
    global cnx
    try:
        cnx.ping(reconnect=True, attempts=3, delay=2)
    except mysql.connector.Error:
        cnx = db_connect()
    return cnx.cursor()


def is_membership_type_valid(membership_type) -> bool:
    """Report whether a portal ``membershipType`` entitles the member to Discord access.

    @param membership_type: The portal's ``membershipType`` for the member — any of the values
        the portal issues (``"Full"``, ``"Trial"``, ``"Monthly"``, ``"None"``, ``"DNR"``,
        ``"Expelled"``, ``"Resigned"``), or None/absent for records that never had one.
    @return True if the type is one of :data:`VALID_MEMBERSHIP_TYPES`.

    Example: ``is_membership_type_valid("Monthly")`` → ``True``;
    ``is_membership_type_valid("None")`` → ``False``.
    """
    return membership_type in VALID_MEMBERSHIP_TYPES


def is_membership_expired(membership_expiration: str) -> bool:
    expiration_date = datetime.strptime(membership_expiration, "%Y-%m-%d")
    current_date = datetime.today()
    return expiration_date < current_date

@app.route("/oauth/callback")
def oauth_callback():
    """Complete the OAuth flow and record the member's authorization.

    Exchanges the authorization ``code`` for an access token, fetches the member's
    portal profile, and rejects anyone whose membership type is not in
    :data:`VALID_MEMBERSHIP_TYPES`, or whose membership has expired. On success it
    upserts a row into ``user_authorizations`` keyed by the Discord user id resolved
    from the pending ``user_states`` row (matched by ``state`` within a 15-minute
    window).

    It deliberately does NOT delete the ``user_states`` row: the bot's
    ``check_user_states`` loop relies on that row to discover the new authorization,
    assign the configured role, post the welcome message, and then remove the row
    itself. See the inline note below.

    @return A Flask ``(body, status)`` tuple: ``("Success", 200)`` on success, or a
        4xx/5xx error describing why verification failed (missing code, token/profile
        fetch failure, invalid/expired membership, or an expired/unknown state link).
    """
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
        logger.error("Error fetching token: %s", response.text)
        return f"Error fetching token: {response.text}", 500

    token_data = response.json()
    access_token = token_data.get('access_token')

    if not access_token:
        logger.error("No access token received")
        return "Error: No access token received", 500

    # Use the access token to get user info

    user_info_response = requests.get(os.getenv('USER_API_URL'), headers={'Authorization': f'Bearer {access_token}'})

    if user_info_response.status_code !=200:
        logger.error("Error fetching user info: %s", user_info_response.text)
        return f"Error fetching user info: {user_info_response.text}", 500

    user_info = user_info_response.json()
    logger.debug("OAuth callback state: %s", state)
    logger.debug("User info from portal: %s", user_info)
    membership_type = user_info.get('membershipType')
    if is_membership_type_valid(membership_type):
        logger.debug("Valid membership for state %s", state)
    else:
        logger.warning("Invalid membership type %r for state %s", membership_type, state)
        return f"Invalid membership, you must have a Full, Trial or Monthly membership", 400
    if(is_membership_expired(user_info['membershipExpiration'])):
        logger.debug("Membership expired: %s exp %s", user_info['membershipNumber'], user_info['membershipExpiration'])
        return f"Membership expired. Please renew your membership and try again.", 400
    MAX_RETRIES = 5
    DELAY_SECONDS = 0.2
    try:
        cursor=get_cursor()
    except mysql.connector.Error as e:
        logger.error("DB unavailable in oauth_callback: %s", e)
        return "Service temporarily unavailable", 503
    for attempt in range(MAX_RETRIES):
        cursor.execute(
            "SELECT user_id FROM user_states WHERE state = %s AND timestamp > %s",
            (state, datetime.now(timezone.utc) - timedelta(minutes=15))
        )
        logger.debug("Looking up state in user_states")
        result = cursor.fetchone()
        cursor.fetchall()
        logger.debug("State lookup result found: %s", result is not None)
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
    # NOTE: do not delete the user_states row here. The bot's check_user_states loop
    # joins user_authorizations -> user_states -> server_roles to discover that this
    # user just authorized, assigns the configured role + posts the welcome, and then
    # deletes the user_states row itself (within <=60s). Deleting it here removes the
    # row before the loop can ever match it, leaving already-present members without
    # their role. Single-use of the state is preserved by that prompt cleanup and the
    # 15-minute timestamp window checked above.
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
    logger.debug("Redirecting to OAuth provider for state %s", state)
    return redirect(auth_url)



try:
  cnx = db_connect()
except mysql.connector.Error as err:
  if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
    logger.critical("DB auth failed: bad credentials")
  elif err.errno == errorcode.ER_BAD_DB_ERROR:
    logger.critical("DB auth failed: database not found")
  else:
    logger.critical("DB connection error: %s", err)

#cursor=cnx.cursor()

if __name__ == "__main__":
    app.run(host="::", port=5000)
