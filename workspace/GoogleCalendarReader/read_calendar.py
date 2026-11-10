import os
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Scope for read-only access to the calendar
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
CLIENT_SECRET_FILE = r'D:\myprograms\pythonai\client_secret.json'
TOKEN_FILE = 'token.json'

def get_credentials(client_secret_file: str = CLIENT_SECRET_FILE, token_file: str = TOKEN_FILE):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secret_file):
                print(f"Error: Client secret file not found at {client_secret_file}")
                return None
            
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(token_file, 'w') as token:
            token.write(creds.to_json())

    return creds

def get_upcoming_events(service, max_results: int = 10):
    now = datetime.datetime.utcnow().isoformat() + 'Z' 
    events_result = service.events().list(calendarId='primary', timeMin=now,
                                          maxResults=max_results, singleEvents=True,
                                          orderBy='startTime').execute()
    return events_result.get('items', [])

def main():
    creds = get_credentials()
    if not creds:
        return

    try:
        service = build('calendar', 'v3', credentials=creds)

        # Get the next 10 upcoming events
        print('Fetching the upcoming 10 events...')
        events = get_upcoming_events(service, max_results=10)

        if not events:
            print('No upcoming events found.')
            return

        print("\nYour Upcoming Events:")
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            print(f"{start} - {event.get('summary', 'No Title')}")

    except HttpError as error:
        print(f'An error occurred: {error}')

if __name__ == '__main__':
    main()
