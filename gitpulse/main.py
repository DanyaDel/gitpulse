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

app = typer.Typer()
console = Console()

DATA_BRANCH = "gitpulse-data"
STATUS_FILE = "status.json"

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
        
        data = {"users": {}}
        if status_path.exists():
            with open(status_path, "r") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    pass
        
        updated_data = update_func(data)
        
        with open(status_path, "w") as f:
            json.dump(updated_data, f, indent=2)
        
        tmp_repo.index.add([STATUS_FILE])
        if tmp_repo.is_dirty():
            tmp_repo.index.commit(f"Update status: {datetime.now().isoformat()}")
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

    def update(data):
        data["users"][username] = {
            "branch": branch,
            "task": task,
            "status": "working",
            "updated_at": datetime.now().isoformat()
        }
        return data

    sync_data(update)
    console.print(f"[green]Started task:[/green] {task} on branch [blue]{branch}[/blue]")
    notify(f"🚀 {username} started: {task} (branch: {branch})")

@app.command()
def stop():
    """Stop current work."""
    repo = get_repo()
    username = repo.config_reader().get_value("user", "name", "Unknown")

    def update(data):
        if username in data["users"]:
            del data["users"][username]
        return data

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
            requests.post(cfg["discord_webhook"], json={"content": message})
        except Exception as e:
            console.print(f"[red]Discord notification failed: {e}[/red]")
            
    # Telegram
    if "tg_token" in cfg and "tg_chat_id" in cfg:
        try:
            url = f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage"
            requests.post(url, json={"chat_id": cfg["tg_chat_id"], "text": message})
        except Exception as e:
            console.print(f"[red]Telegram notification failed: {e}[/red]")

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
