import os
import json
import shutil
import tempfile
from datetime import datetime
from typing import Optional
from pathlib import Path

import typer
import git
from rich.console import Console
from rich.table import Table
import requests
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer()
console = Console()

DATA_BRANCH = "gitpulse-data"
STATUS_FILE = "status.json"
HISTORY_FILE = "history.json"
DASHBOARD_FILE = "index.html"

def get_repo():
    try:
        return git.Repo(os.getcwd(), search_parent_directories=True)
    except git.InvalidGitRepositoryError:
        console.print("[red]Error: Not a git repository.[/red]")
        raise typer.Exit(1)

def get_gitpulse_dir():
    repo = get_repo()
    gp_dir = Path(repo.git_dir) / "gitpulse"
    gp_dir.mkdir(exist_ok=True)
    return gp_dir

def load_config():
    gp_dir = get_gitpulse_dir()
    config_file = gp_dir / "config.json"
    if config_file.exists():
        with open(config_file, "r") as f:
            return json.load(f)
    return {}

def save_config(config):
    gp_dir = get_gitpulse_dir()
    config_file = gp_dir / "config.json"
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

def sync_data(update_func):
    repo = get_repo()
    tmp_dir = tempfile.mkdtemp()
    try:
        # Check if remote branch exists
        has_remote = False
        try:
            remote = repo.remote("origin")
            remote.fetch()
            has_remote = True
        except (ValueError, git.GitCommandError, AttributeError):
            pass
        
        # Clean up any stale worktrees for this branch
        try:
            worktrees = repo.git.worktree("list").split("\n")
            for wt in worktrees:
                if DATA_BRANCH in wt:
                    wt_path = wt.split()[0]
                    repo.git.worktree("remove", "--force", wt_path)
        except Exception:
            pass

        # Create a temporary worktree for the data branch
        try:
            # Check if branch exists locally
            repo.git.rev_parse("--verify", DATA_BRANCH)
            repo.git.worktree("add", tmp_dir, DATA_BRANCH)
        except git.GitCommandError:
            # If branch doesn't exist locally, try to create it from remote or as orphan
            if has_remote and f"origin/{DATA_BRANCH}" in [ref.name for ref in repo.references]:
                repo.git.worktree("add", "-b", DATA_BRANCH, tmp_dir, f"origin/{DATA_BRANCH}")
            else:
                # Create orphan
                repo.git.worktree("add", "--detach", tmp_dir)
                tmp_repo = git.Repo(tmp_dir)
                tmp_repo.git.checkout("--orphan", DATA_BRANCH)
                tmp_repo.git.rm("-rf", ".")
        
        tmp_repo = git.Repo(tmp_dir)
        status_path = Path(tmp_dir) / STATUS_FILE
        history_path = Path(tmp_dir) / HISTORY_FILE
        
        # Load Status
        data = {"users": {}}
        if status_path.exists():
            with open(status_path, "r") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    pass
        
        # Load History
        history = []
        if history_path.exists():
            with open(history_path, "r") as f:
                try:
                    history = json.load(f)
                except json.JSONDecodeError:
                    pass
        
        updated_data, updated_history = update_func(data, history)
        
        with open(status_path, "w") as f:
            json.dump(updated_data, f, indent=2)
            
        with open(history_path, "w") as f:
            json.dump(updated_history, f, indent=2)
        
        tmp_repo.index.add([STATUS_FILE, HISTORY_FILE])
        if tmp_repo.is_dirty():
            tmp_repo.index.commit(f"Update GitPulse data: {datetime.now().isoformat()}")
            if has_remote:
                try:
                    repo.remotes.origin.push(DATA_BRANCH)
                    console.print("[green]Status synced to remote.[/green]")
                except Exception as e:
                    console.print(f"[yellow]Warning: Could not push to remote: {e}[/yellow]")
            else:
                console.print("[yellow]Status updated locally (no remote origin found).[/yellow]")
        
    finally:
        try:
            repo.git.worktree("remove", "--force", tmp_dir)
        except Exception:
            pass
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        repo.git.worktree("prune")

@app.command()
def init():
    """Initialize GitPulse in the current repository."""
    repo = get_repo()
    console.print(f"Initializing GitPulse in [blue]{repo.working_dir}[/blue]...")
    
    # Ensure data branch exists
    try:
        repo.git.rev_parse("--verify", DATA_BRANCH)
        console.print(f"Branch [yellow]{DATA_BRANCH}[/yellow] already exists.")
    except git.GitCommandError:
        console.print(f"Creating orphaned branch [yellow]{DATA_BRANCH}[/yellow]...")
        current = repo.active_branch
        repo.git.checkout("--orphan", DATA_BRANCH)
        repo.git.rm("-rf", ".")
        status_path = Path(repo.working_dir) / STATUS_FILE
        with open(status_path, "w") as f:
            json.dump({"users": {}}, f)
        repo.index.add([STATUS_FILE])
        repo.index.commit("Initial GitPulse commit")
        
        try:
            repo.remotes.origin.push(DATA_BRANCH)
            console.print("[green]Branch pushed to remote.[/green]")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not push to remote: {e}[/yellow]")
            
        current.checkout()

    console.print("[bold green]GitPulse initialized successfully![/bold green]")

@app.command()
def start(task: str = typer.Argument(..., help="What are you working on?")):
    """Start working on a task."""
    repo = get_repo()
    username = repo.config_reader().get_value("user", "name", "Unknown")
    branch = repo.active_branch.name

    # Pre-check for other users on this branch
    try:
        has_remote = False
        try:
            repo.remotes.origin.fetch(DATA_BRANCH)
            has_remote = True
        except: pass
        
        ref = f"origin/{DATA_BRANCH}" if has_remote else DATA_BRANCH
        content = repo.git.show(f"{ref}:{STATUS_FILE}")
        current_data = json.loads(content)
        
        others = [u for u, info in current_data.get("users", {}).items() 
                  if info.get("branch") == branch and u != username]
        if others:
            console.print(f"[bold yellow]Warning:[/bold yellow] Users [cyan]{', '.join(others)}[/cyan] are already working on branch [blue]{branch}[/blue]!")
            if not typer.confirm("Do you want to proceed?"):
                raise typer.Exit()
    except Exception:
        pass

    def update(data, history):
        data["users"][username] = {
            "branch": branch,
            "task": task,
            "status": "working",
            "updated_at": datetime.now().isoformat()
        }
        data = update_tg_dashboard(data)
        return data, history

    sync_data(update)
    console.print(f"[green]Started task:[/green] {task} on branch [blue]{branch}[/blue]")
    notify(f"🚀 {username} started: {task} (branch: {branch})")

@app.command()
def stop():
    """Stop current work."""
    repo = get_repo()
    username = repo.config_reader().get_value("user", "name", "Unknown")

    def update(data, history):
        if username in data["users"]:
            user_data = data["users"][username]
            start_time = user_data.get("updated_at")
            end_time = datetime.now().isoformat()
            
            # Record to history
            history.append({
                "user": username,
                "task": user_data.get("task"),
                "branch": user_data.get("branch"),
                "started_at": start_time,
                "stopped_at": end_time
            })
            
            del data["users"][username]
            data = update_tg_dashboard(data)
        return data, history

    sync_data(update)
    console.print("[yellow]Stopped current work status.[/yellow]")
    notify(f"🏁 {username} stopped working.")

@app.command()
def status():
    """Show current status of all developers."""
    repo = get_repo()
    
    # Try to fetch latest data if remote exists
    has_remote = False
    try:
        repo.remotes.origin.fetch(DATA_BRANCH)
        has_remote = True
    except (AttributeError, ValueError, git.GitCommandError):
        pass
    
    # Read from data branch
    try:
        ref = f"origin/{DATA_BRANCH}" if has_remote else DATA_BRANCH
        content = repo.git.show(f"{ref}:{STATUS_FILE}")
        data = json.loads(content)
    except Exception:
        console.print("[red]Could not read status data. Have you run 'gitpulse init'?[/red]")
        return

    table = Table(title="GitPulse Repository Status")
    table.add_column("Developer", style="cyan")
    table.add_column("Branch", style="magenta")
    table.add_column("Task", style="green")
    table.add_column("Last Update", style="dim")

    for user, info in data.get("users", {}).items():
        table.add_row(
            user,
            info.get("branch", "N/A"),
            info.get("task", "N/A"),
            info.get("updated_at", "N/A")
        )

    console.print(table)

@app.command()
def log(limit: int = typer.Option(10, help="Number of entries to show")):
    """Show work history and time spent."""
    repo = get_repo()
    
    # Try to fetch latest data if remote exists
    has_remote = False
    try:
        repo.remotes.origin.fetch(DATA_BRANCH)
        has_remote = True
    except (AttributeError, ValueError, git.GitCommandError):
        pass
    
    # Read from data branch
    try:
        ref = f"origin/{DATA_BRANCH}" if has_remote else DATA_BRANCH
        content = repo.git.show(f"{ref}:{HISTORY_FILE}")
        history = json.loads(content)
    except Exception:
        console.print("[yellow]No history found yet.[/yellow]")
        return

    table = Table(title="GitPulse Work History")
    table.add_column("Developer", style="cyan")
    table.add_column("Task", style="green")
    table.add_column("Duration", style="bold white")
    table.add_column("Started", style="dim")
    table.add_column("Stopped", style="dim")

    # Show most recent first
    for entry in reversed(history[-limit:]):
        start = datetime.fromisoformat(entry["started_at"])
        stop = datetime.fromisoformat(entry["stopped_at"])
        duration = stop - start
        
        # Format duration
        hours, remainder = divmod(int(duration.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m {seconds}s"

        table.add_row(
            entry.get("user", "Unknown"),
            entry.get("task", "N/A"),
            duration_str,
            start.strftime("%Y-%m-%d %H:%M"),
            stop.strftime("%Y-%m-%d %H:%M")
        )

    console.print(table)

@app.command()
def config(
    discord_webhook: Optional[str] = typer.Option(None, "--discord", help="Discord Webhook URL"),
    tg_token: Optional[str] = typer.Option(None, "--tg-token", help="Telegram Bot Token"),
    tg_chat_id: Optional[str] = typer.Option(None, "--tg-chat", help="Telegram Chat ID")
):
    """Configure notifications."""
    cfg = load_config()
    if discord_webhook: cfg["discord_webhook"] = discord_webhook
    if tg_token: cfg["tg_token"] = tg_token
    if tg_chat_id: cfg["tg_chat_id"] = tg_chat_id
    
    save_config(cfg)
    console.print("[green]Configuration updated.[/green]")

def notify(message: str):
    cfg = load_config()
    
    # Discord
    if "discord_webhook" in cfg:
        try:
            resp = requests.post(cfg["discord_webhook"], json={"content": message}, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    error_msg = f"{e.response.status_code}: {e.response.json().get('description', e.response.text)}"
                except:
                    error_msg = f"{e.response.status_code}: {e.response.text}"
            console.print(f"[red]Discord notification failed: {error_msg}[/red]")
            
    # Telegram (Simple Notification)
    if "tg_token" in cfg and "tg_chat_id" in cfg:
        try:
            url = f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage"
            resp = requests.post(url, json={"chat_id": cfg["tg_chat_id"], "text": message}, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            pass # Silent fail for simple notify unless it's test-notify

def update_tg_dashboard(data):
    """Updates or creates a pinned-like live status message in Telegram."""
    cfg = load_config()
    if not ("tg_token" in cfg and "tg_chat_id" in cfg):
        return data

    token = cfg["tg_token"]
    chat_id = cfg["tg_chat_id"]
    msg_id = data.get("tg_live_message_id")

    # Format the message
    lines = [
        "<b>🚀 GitPulse Live Dashboard</b>",
        f"<i>Last Update: {datetime.now().strftime('%H:%M:%S')}</i>",
        "─" * 15
    ]

    active_users = data.get("users", {})
    if not active_users:
        lines.append("\n☕ <b>All quiet.</b> No active tasks.")
    else:
        for user, info in active_users.items():
            start_time = datetime.fromisoformat(info["updated_at"])
            duration = datetime.now() - start_time
            hours, remainder = divmod(int(duration.total_seconds()), 3600)
            mins, _ = divmod(remainder, 60)
            dur_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            
            lines.append(f"\n👤 <b>{user}</b>")
            lines.append(f"┗ 📝 {info['task']}")
            lines.append(f"┗ 🌿 <code>{info['branch']}</code> ({dur_str})")

    full_text = "\n".join(lines)

    try:
        if msg_id:
            # Try to edit existing message
            url = f"https://api.telegram.org/bot{token}/editMessageText"
            payload = {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": full_text,
                "parse_mode": "HTML"
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 400 and "message is not modified" in resp.text:
                return data
            if not resp.ok:
                # If message not found, create new
                msg_id = None

        if not msg_id:
            # Create new message
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": full_text,
                "parse_mode": "HTML"
            }
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            new_msg_id = resp.json().get("result", {}).get("message_id")
            data["tg_live_message_id"] = new_msg_id
            
            # Optionally pin it
            try:
                pin_url = f"https://api.telegram.org/bot{token}/pinChatMessage"
                requests.post(pin_url, json={"chat_id": chat_id, "message_id": new_msg_id}, timeout=5)
            except: pass

    except Exception as e:
        console.print(f"[red]Telegram Dashboard update failed: {e}[/red]")
    
    return data

@app.command()
def test_notify():
    """Send a test notification to verify configuration."""
    cfg = load_config()
    if not cfg.get("discord_webhook") and not (cfg.get("tg_token") and cfg.get("tg_chat_id")):
        console.print("[yellow]No notifications configured. Use 'gitpulse config' to set them up.[/yellow]")
        return
        
    console.print("Sending test notification...")
    notify("🔔 This is a test notification from GitPulse!")
    console.print("[green]Test notification process completed.[/green]")

@app.command()
def send(msg: str = typer.Argument(..., help="Message to send")):
    """Send a custom notification message."""
    notify(msg)
    console.print(f"[green]Notification sent:[/green] {msg}")

@app.command()
def install_hooks():
    """Install git hooks to ensure GitPulse is used."""
    repo = get_repo()
    hook_path = Path(repo.git_dir) / "hooks" / "pre-commit"
    
    hook_content = f"""#!/bin/sh
# GitPulse pre-commit hook
# Try to find gitpulse executable
GP_EXEC=$(which gitpulse)
if [ -z "$GP_EXEC" ]; then
    # Fallback to common locations or just assume it's in path
    GP_EXEC="gitpulse"
fi

username=$(git config user.name)
gitpulse_status=$($GP_EXEC status | grep "$username")

if [ -z "$gitpulse_status" ]; then
    echo "❌ Error: You haven't started a task in GitPulse!"
    echo "Run 'gitpulse start \"your task\"' before committing."
    exit 1
fi
"""
    
    with open(hook_path, "w") as f:
        f.write(hook_content)
    
    os.chmod(hook_path, 0o755)
    console.print("[green]Pre-commit hook installed.[/green]")

if __name__ == "__main__":
    app()
