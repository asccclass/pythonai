import subprocess
import sys
import os

def test_environment():
    """Check if required libraries are installed."""
    package_to_module = {
        'google-auth-oauthlib': 'google_auth_oauthlib',
        'google-api-python-client': 'googleapiclient',
        'google-auth-httplib2': 'google_auth_httplib2',
    }
    missing = []
    
    for pkg, mod in package_to_module.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
            
    assert not missing, f"Missing packages: {missing}"

if __name__ == "__main__":
    try:
        test_environment()
        print("Environment check passed. Ready to run read_calendar.py")
        sys.exit(0)
    except AssertionError as e:
        print(e)
        sys.exit(1)
