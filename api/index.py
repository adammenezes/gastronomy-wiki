import sys
import os

# Ensure the root directory is in sys.path so we can import from agent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.cli.server import app

# Vercel will look for the 'app' variable in this file
