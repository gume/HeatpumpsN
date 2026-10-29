# Agent Instructions & Constraints

## Environment & Git Setup
- **Workspace Scope:** Do not write anywhere else; use this folder only. Other folders may be read freely.
- **Python Environment:** A virtual environment is set up at `.venv`. Install all missing libraries there. Prefer using `uv`, but `pip` should work as a fallback.
- **Version Control:** Version controll extsis. As an exception, you can make changes in the .gitignore file outside of this folder. Make essential commits, but do not push.

# Tests and Run
- The controller might not always available, but for most of the time it should be. There is a reverse proxy to access it. From this machine use the localhost:30080 to access it. Use it freely for testing or sampling messages.
- MQTT broker is running on localhost:31883
- If tests are created, put it within a tests folder undir this folder.  
