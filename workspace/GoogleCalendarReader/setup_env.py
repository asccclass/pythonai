import subprocess
import sys

def install_dependencies():
    packages = ['google-auth-oauthlib', 'google-api-python-client', 'google-auth-httplib2']
    for pkg in packages:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

if __name__ == "__main__":
    try:
        install_dependencies()
        print("Dependencies installed successfully.")
    except Exception as e:
        print(f"Error installing dependencies: {e}")
        sys.exit(1)
