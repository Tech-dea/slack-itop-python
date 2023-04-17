import os
import logging
import requests
import json
import re
import multiprocessing
import html
import sqlite3
import time
import datetime
import pytz
from slack_bolt import App
from dotenv import load_dotenv
from flask import Flask, request

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load the environment variables from the .env file
load_dotenv()

# Initialize the slack app
slack_app = App(token=os.environ['SLACK_BOT_TOKEN'])
slack_user_app = App(token=os.environ['SLACK_USER_TOKEN'])
slack_group_id = os.environ['SLACK_GROUP_ID']
slack_support_channel_id = os.environ['SLACK_SUPPORT_CHANNEL_ID']

# File to store the list of ticketed threads, this is to keep track of the icon
# For local testing only
ticketed_threads_file = 'ticketed_threads.txt'
# For server use, perform change base on your hosting location
#ticketed_threads_file = '/home/ec2-user/slackbotdata/ticketed_threads.txt'

# Helper to establish a connection to the database
def get_db_connection():
    # Create a connection to the database
    # Use this for local testing only
    conn = sqlite3.connect('user_db.sqlite')
    # This is for hosting use, perform change base on your hosting location
    #conn = sqlite3.connect('/home/ec2-user/slackbotdata/user_db.sqlite')
    return conn

# Load the ticketed threads from file
def load_ticketed_threads():
    ticketed_threads = set()
    if os.path.exists(ticketed_threads_file):
        with open(ticketed_threads_file, 'r') as f:
            for line in f:
                ticketed_threads.add(line.rstrip())
    return ticketed_threads

# Save the ticketed threads to the file
def save_ticketed_threads(ticketed_threads):
    with open(ticketed_threads_file, 'w') as f:
        for thread_ts in ticketed_threads:
            f.write(str(thread_ts) + "\n")

# Get the assigned user ID from slack
def get_assigned_user_id(name):
    conn = get_db_connection()
    c = conn.cursor()

    # Query the database for the user ID based on the real name
    c.execute("SELECT id FROM users WHERE real_name = ?", (name,))
    result = c.fetchone()

    # Close the database connection
    conn.close()
    
    # Return None if the user was not found
    if result:
        return result[0]
    else:
        return None

# Helper function to store user information in the database
def store_user_info(client, channel_id, user_id):
    cursor = None
    users = []

    # Paginate through the users in the workspace
    while True:
        response = client.users_list(cursor=cursor)
        users += response["members"]
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Store the IDs of all active, non-deleted users in a list
    active_user_ids = [user["id"] for user in users if not user["deleted"]]

    conn = get_db_connection()
    c = conn.cursor()

    # Create the users table if it doesn't already exist
    c.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,real_name TEXT, email TEXT, is_admin INTEGER)")

    # Store the user information in the database
    for user_info in users:
        # Skip guest accounts and bots
        if user_info['is_bot'] or user_info['is_app_user']:
            continue

        if user_info['id'] in active_user_ids:
            # Check if email and is_admin are present in the user's info
            email = user_info["profile"].get("email", "")
            is_admin = user_info.get("is_admin", 0)
            if is_admin:
                is_admin = int(is_admin)
            username = user_info["name"]
            first_name = user_info["profile"].get("first_name", "")
            last_name = user_info["profile"].get("last_name", "")
            c.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?, ?)", (user_info["id"], username, first_name, last_name, user_info["profile"]["real_name"], email, is_admin))
    conn.commit()
    conn.close()

    # Send a confirmation message
    client.chat_postEphemeral(channel=channel_id, user=user_id, text="User information has been stored in the database")

# Helper function to check if user is an admin
def is_user_admin(user_id):
    # Check if the database file already exists
    if os.path.isfile('user_db.sqlite'):
    #if os.path.isfile('/home/ec2-user/slackbotdata/user_db.sqlite'):
        # If the database file exists, check if the user is an admin using the database
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT is_admin FROM users WHERE id=?", (user_id,))
        user_info = c.fetchone()
        conn.close()
        if user_info:
            return user_info[0]
        else:
            return False
    else:
        # If the database file does not exist or the user is not an admin in the database, check using the Slack API
        user_info = slack_app.client.users_info(user=user_id)["user"]
        return user_info.get("is_admin", False)

# Handler function for get user information from database
def get_user_info_from_db(user_id):
    conn = get_db_connection()
    c = conn.cursor()

    # Query the database for the user's information
    c.execute("SELECT first_name, last_name FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()

    # Close the connection
    conn.close()

    # Return the user's information as a tuple
    return result or ("","")

# Send the whole conversation back to iTop
def send_conversation_to_itop(conversation, ticket_number):
    # Extract the necessary information from the conversation
    output = conversation['messages']
    first_message = True
    for message in output:
        if message.get('user') and message.get('text') and not message.get('bot_id'):
            if first_message:
                first_message = False
                continue
            text = message['text']
            #user = message['user']
    
            # Format the data for itop API
            url = os.getenv('ITOP_API_ENDPOINT')
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': 'Basic ' + str(os.getenv('BASIC_AUTHENTICATION'))
            }
            data = {
                "operation": "core/update",
                "comment": "Ticket update by Slack Bot",
                "class": "UserRequest",
                "key": {
                    "ref": ticket_number,
                },
                "output_fields": "id, friendlyname",
                "fields": {
                    "public_log": f'{text}',
                }
            }

            # Send the data to the iTop API
            json_data = json.dumps(data)
            response = requests.post(url, headers=headers, data={'json_data': json_data})

            # Check if the API call was successful
            if response.status_code != 200:
                logger.error("Failed to send conversation to iTop: " + response.text)
                break
            else:
                logger.info("Successfully sent conversation to iTop")

def run_slack_app():
    slack_app.start(3000)

# Being tagged to raise ticket
@slack_app.event('app_mention')
def raise_ticket(ack, event, logger):
    # Acknowledge the event
    ack()

    # Get channel, message and thread information
    channel_id = event['channel']
    thread_ts = event.get('thread_ts') or event['ts']

    # Load the list of ticketed threads
    ticketed_threads = load_ticketed_threads()

    # Check if the current thread is in the list of ticketed threads
    if thread_ts in ticketed_threads:
        reply_text = f"Hi <@{event['user']}>, we are resolving the ticket... Once we have any update, you will see it here. Stay tuned."
        slack_app.client.chat_postMessage(channel=channel_id, text=reply_text, thread_ts=thread_ts)
        return

    # Add the current thread to the list of ticketed threads
    ticketed_threads.add(thread_ts)
    save_ticketed_threads(ticketed_threads)

    # Get slack message information
    if thread_ts:
        thread_ts_fixed = "".join(thread_ts.split('.'))
    slack_address = "https://xxx.slack.com/archives/" + channel_id + "/p" + thread_ts_fixed

    # Get submit user's real name
    user_id = event['user']
    first_name, last_name = get_user_info_from_db(user_id)

    # Get the message
    message = event['text']

    # Assign title to the message
    if 'slack' in message.lower() or 'Slack' in message:
        title = 'Slack issue'
    elif 'upload' in message or 'zoom.us' in message:
        title = 'Upload Consultation'
    elif 'Shadowsocks' in message.lower() or 'shadowsocks' in message or 'cannot connect to IPS' in message:
        title = 'Shadowsocks issue'
    elif 'account' in message or 'disabled' in message:
        title = 'Google account disabled'
    elif 'magic link' in message.lower() or 'update email' in message.lower() or 'change the email id' in message.lower():
        title = 'Expert / client email ID change request'
    elif 'TnC' in message or 'renew' in message:
        title = 'TnC Issues'
    elif 'hourly rate' in message:
        title = 'Financial rate change request'
    else:
        title = ''

    # Extract the description and hyperlink from the message
    description = message

    # Find URLs in description and replace them with HTML hyperlinks
    url_pattern = r'(https?://\S+)'
    hyperlinks = re.findall(url_pattern, description)
    for hyperlink in hyperlinks:
        hyperlink = hyperlink.rstrip('>')
        description = description.replace(hyperlink, hyperlink)

    words_pattern = r'<([^>]+)>'
    words = re.findall(words_pattern, message)
    for word in words:
        # This is to make sure the tagged word also in the message
        if word == '@XXXXXXXXXX':
            description = description.replace(f'<{word}>', 'xxx Ticketing System')
        else:
            description = description.replace(f'<{word}>', word)

    # Enclose the description in a <p> tag
    description = html.escape(description)

    # iTop parameter for creating the ticket
    url = os.getenv('ITOP_API_ENDPOINT')
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': 'Basic ' + str(os.getenv('BASIC_AUTHENTICATION'))
    }
    data = {
        "operation": "core/create",
        "comment": "Ticket create by Slack Bot",
        "class": "UserRequest",
        "output_fields": "id, friendlyname",
        "fields": {
            "org_id": "SELECT Organization WHERE name = \"xxx\"",
            "caller_id": {
                "name": last_name,
                "first_name": first_name
            },
            "description": description,
            "title": title,
            "slack_address": slack_address
        }
    }

    # Send the request to iTop and make sure there is a JSON response
    json_data = json.dumps(data)
    response = requests.post(url, headers=headers, data={'json_data': json_data})
    if response.status_code != 200:
        logger.error(f"Failed to create iTop ticket: {response.text}")
        return

    # Get the ticket friendly name
    response_json = response.json()
    objects = response_json.get("objects")
    user_request = list(objects.values())[0]
    ticket_friendly_name = user_request["fields"]["friendlyname"]

    # Reply the message with the ticket number
    reply_text = f"Hi <@{event['user']}>, your request has been received and a ticket has been created with ticket number {ticket_friendly_name}. You will be updated with the progress of this ticket."
    slack_app.client.chat_postMessage(channel=channel_id, text=reply_text, thread_ts=thread_ts)

# Slash command to store user information in the database
@slack_app.command("/store-user-info")
def handle_store_user_info(ack, body, logger):
    # Acknowledge the command
    ack()

    # Check if the user is an admin
    if not is_user_admin(body["user_id"]):
        ack("You do not have sufficient permissions to run this command.")
        return
    
    # Store the user information in the database
    store_user_info(slack_app.client, body["channel_id"], body["user_id"])

# Slash command to update user information in the database
@slack_app.command("/update-user-info")
def handle_update_user_info(ack, body):
    # Acknowledge the command
    ack()

    # Check if the user is an admin
    if not is_user_admin(body["user_id"]):
        slack_app.client.chat_postEphemeral(user=body["user_id"], channel=body["channel_id"], text="You do not have sufficient permissions to run this command.")
        return
    
    # Initialize the cursor for paginating through the users in the workspace
    cursor = None
    users = []

    # Paginate through the users in the workspace
    while True:
        response = slack_app.client.users_list(cursor=cursor)
        users += response["members"]
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Store the IDs of all active, non-deleted users in a list
    active_user_ids = [user["id"] for user in users if not user["deleted"]]

    # Create a connection to the database
    conn = get_db_connection()
    c = conn.cursor()
        
    # Store information about new active users in the database
    admin_user_ids = []
    for user_info in users:
        # Skip guest accounts and bots
        if user_info['is_bot'] or user_info['is_app_user']:
            continue

        if user_info['id'] in active_user_ids:
            # Check if email and is_admin are present in the user's info
            email = user_info["profile"].get("email", "")
            first_name = user_info["profile"].get("first_name", "")
            last_name = user_info["profile"].get("last_name", "")
            username = user_info["name"]
            is_admin = 0
            # Check if the user is an admin
            if user_info["id"] in active_user_ids and user_info["is_admin"]:
                is_admin = 1
                admin_user_ids.append(user_info["id"])
            c.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?, ?)", (user_info["id"], username, first_name, last_name, user_info["profile"]["real_name"], email, is_admin))
    
    # Delete users that are inactive in the workspace or have invalid email addresses
    invalid_email_domains = ("@xxx.com", "@xxxx.com")
    inactive_and_invalid_user_ids = c.execute("SELECT id FROM users WHERE id NOT IN ({}) OR email NOT LIKE '%{}' AND email NOT LIKE '%{}'".format(", ".join("?" for _ in active_user_ids), *invalid_email_domains), active_user_ids).fetchall()
    if inactive_and_invalid_user_ids:
        # Flattern the list of tuples and extract the first item of each tuple
        inactive_and_invalid_user_ids = [user_id[0] for user_id in inactive_and_invalid_user_ids]
        # Remove empty values and duplicates
        inactive_and_invalid_user_ids = list(set([user_id for user_id in inactive_and_invalid_user_ids if user_id]))
        c.execute("DELETE FROM users WHERE id IN ({})".format(", ".join("?" for _ in inactive_and_invalid_user_ids)), inactive_and_invalid_user_ids)

    conn.commit()
    conn.close()

    # Send a confirmation message
    slack_app.client.chat_postEphemeral(user=body["user_id"], channel=body["channel_id"],text="User information has been updated in the database")

# Receive incoming webhook message and check if the ticket is resolved.
def run_flask_app(port):
    flask_app = Flask(__name__)

    # Flow for ticket assigned, including adding eyes as indicator for the ticket being noticed
    @flask_app.route('/ticketassigned', methods=['POST'])
    def ticket_assigned():
        # Get the data from the incoming webhook
        data = request.get_json()

        # Extract the 'text' key from the first item in the 'blocks' list
        blocks = data['blocks']
        text_block = blocks[0]['text']
        text = text_block['text']

        # Find the index of the 'Link to Slack' string
        start_index = text.find("Link to Slack:")

        # Extract the link by slicing the text from the start index to the end
        link = text[start_index:].strip()

        # Extract the slack address from the link
        slack_address = link.split("/p")[1].strip()

        # Add a period after the 10-digit slack address
        slack_address = slack_address[:10] + "." + slack_address[10:]

        # Extract the channel ID from the link
        channel_id = link.split("/")[-2].strip()

        # Get the first post in the thread
        thread_response = slack_app.client.conversations_history(channel=channel_id, oldest=slack_address, limit=1, inclusive=True)
        messages = thread_response["messages"]

        # Extract the information from the message
        for message in messages:
            user_id = message["user"]

        # Extract the ticket number from the json result
        start_ticket = text.find("R-")
        end_ticket = start_ticket + 8
        ticket_number = text[start_ticket:end_ticket]

        # Extract the assigned person name from JSON
        assigned_person_start_index = text.find("Assigned to:")
        newline_index = text.find("\n", assigned_person_start_index)
        assigned_person_name = text[assigned_person_start_index:newline_index].split("Assigned to:")[1].strip()

        # Find the assigned person's user ID in slack
        assigned_user_id = get_assigned_user_id(assigned_person_name)

        # Check if the first post of the thread already has the :eyes: emoji
        try:
            reactions_response = slack_app.client.reactions_get(channel=channel_id, timestamp=slack_address)
            if "message" in reactions_response:
                reactions = reactions_response["message"].get("reactions", [])
                eyes_reaction_exists = False
                for reaction in reactions:
                    if reaction["name"] == "eyes":
                        eyes_reaction_exists = True
                        break
        
                # Add the :eyes: reaction to the first post of the thread if it doesn't already have it
                if not eyes_reaction_exists:
                    slack_app.client.reactions_add(name="eyes", channel=channel_id, timestamp=slack_address)
        except:
            # Handle any exceptions that may occur
            print("Error checking for reactions")

        # Reply to the thread saying "Your ticket is assigned to specific engineer with tag"
        slack_app.client.chat_postMessage(channel=channel_id, text=f"Hi <@{user_id}>, your ticket is assigned to {assigned_person_name}.", thread_ts=slack_address)

        # Send a direct message to the assigned user
        direct_message_channel = slack_app.client.conversations_open(users=assigned_user_id)
        direct_message_channel_id = direct_message_channel["channel"]["id"]
        slack_app.client.chat_postMessage(channel=direct_message_channel_id, text=f"Hi <@{assigned_user_id}>, you have been assigned to {ticket_number}. \n\n{link}")
        
        #return a success status code
        return ' ', 200

    # Flow for ticket resolved, including mark tick and send back all the conversation to itop
    @flask_app.route('/ticketresolve', methods=['POST'])
    def ticket_resolved():
        # Get the data from the incoming webhook
        data = request.get_json()
        print(data)

        # Extract the 'text' key from the first item in the 'blocks' list
        blocks = data['blocks']
        text_block = blocks[0]['text']
        text = text_block['text']

        # Find the index of the 'Link to Slack' string
        start_index = text.find("Link to Slack:")

        # Extract the link by slicing the text from the start index to the end
        link = text[start_index:].strip()

        # Extract the slack address from the link
        slack_address = link.split("/p")[1].strip()

        # Add a period after the 10-digit slack address
        slack_address = slack_address[:10] + "." + slack_address[10:]

        # Extract the channel ID from the link
        channel_id = link.split("/")[-2].strip()

        # Extract the ticket number from the json result
        start_ticket = text.find("R-")
        end_ticket = start_ticket + 8
        ticket_number = text[start_ticket:end_ticket]
        print(ticket_number)

        # Get the first post in the thread
        thread_response = slack_app.client.conversations_history(channel=channel_id, oldest=slack_address, limit=1, inclusive=True)
        messages = thread_response["messages"]

        # Extract the information from the message
        for message in messages:
            user_id = message["user"]

        # Add the :white_check_mark: emoji to the first post of the thread
        slack_app.client.reactions_add(name="white_check_mark", channel=channel_id, timestamp=slack_address)

        # Remove the :eye: emoji from the first post of the thread
        slack_app.client.reactions_remove(name="eyes", channel=channel_id, timestamp=slack_address)

        # Reply to the thread saying "Your ticker has been resolved, please check"
        slack_app.client.chat_postMessage(channel=channel_id, text=f"Hi <@{user_id}>, your ticket has been resolved, please check.", thread_ts=slack_address)

        # Delete the thread_ts entry from the ticketed_threads.txt file
        with open(ticketed_threads_file, "r") as file:
            lines = file.readlines()
        lines = [line for line in lines if slack_address not in line]
        with open(ticketed_threads_file, "w") as file:
            file.writelines(lines)

        # Send the conversation back to iTop
        conversation = slack_app.client.conversations_replies(channel=channel_id, ts=slack_address)
        send_conversation_to_itop(conversation, ticket_number)

        #return a success status code
        return ' ', 200

    flask_app.run(port=port)

if __name__ == '__main__':
    t1 = multiprocessing.Process(target=run_slack_app)
    t2 = multiprocessing.Process(target=run_flask_app, args=(3001,))
    t1.start()
    t2.start()
