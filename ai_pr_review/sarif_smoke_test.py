"""Smoke-test module: deliberately flawed code to exercise SARIF analyzers.

DO NOT use in production. This file exists solely to verify that the
sarif-prep CI job (Ruff, Semgrep, CodeQL) ingests findings end-to-end
and surfaces them in the AI review comment.
"""

import subprocess
import sqlite3
import hashlib  # noqa: F401 (intentional unused import for Ruff F401)

# Ruff E501: line too long
VERY_LONG_LINE = "this is a deliberately long line that exceeds the 88-character line length limit enforced by ruff in this project"

# Ruff E711: comparison to None using == instead of `is`
def check_value(val):
    if val == None:
        return False
    return True


# Semgrep + CodeQL: command injection — subprocess with shell=True and unsanitized user input
def run_user_command(user_input):
    result = subprocess.run(user_input, shell=True, capture_output=True)
    return result.stdout


# CodeQL: SQL injection — user input concatenated directly into query string
def get_user_record(db_path, username):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    cursor.execute(query)
    return cursor.fetchall()


# Semgrep: hardcoded credentials
AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
GITHUB_TOKEN = "ghp_aaaBBBcccDDDeeeFFFgggHHHiiiJJJ012345"

# Ruff E302: expected 2 blank lines before function definition
def helper():
    pass
