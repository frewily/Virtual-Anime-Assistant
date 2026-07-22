def get_foreground_app() -> dict | None:
    import subprocess

    try:
        script = '''
        tell application "System Events"
            set frontApp to first application process whose frontmost is true
            set appName to name of frontApp
        end tell
        return appName
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        app_name = result.stdout.strip()
        if not app_name:
            return None
        return {"appName": app_name, "appId": app_name}
    except Exception:
        return None
