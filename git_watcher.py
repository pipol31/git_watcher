#!/usr/bin/env python3
"""
Daemon de surveillance de dépôts Git avec auto-pull et vraie démonisation.
"""

import subprocess
import time
import logging
import sys
import signal
import argparse
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import yaml

from daemonize import Daemonizer


@dataclass
class RepoConfig:
    name: str
    path: str
    remote: str = "origin"
    branch: str = "main"
    auto_pull: bool = False
    require_clean: bool = True
    allow_diverged_pull: bool = False


class GitRepoChecker:
    """Gère les opérations Git pour un dépôt donné."""

    def __init__(self, repo_config: RepoConfig, logger: logging.Logger):
        self.config = repo_config
        self.logger = logger
        self.path = Path(repo_config.path).expanduser().resolve()

    def _run_git_command(self, args: list[str], timeout: int = 60) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path)] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            self.logger.error(
                f"[{self.config.name}] Erreur commande git {' '.join(args)}: {e.stderr.strip()}"
            )
            return None
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"[{self.config.name}] Timeout sur la commande git {' '.join(args)}"
            )
            return None
        except FileNotFoundError:
            self.logger.error("Commande git introuvable. Git est-il installé ?")
            return None

    def is_valid_repo(self) -> bool:
        if not self.path.exists():
            self.logger.error(f"[{self.config.name}] Chemin inexistant: {self.path}")
            return False
        result = self._run_git_command(["rev-parse", "--is-inside-work-tree"])
        return result == "true"

    def fetch(self) -> bool:
        result = self._run_git_command(
            ["fetch", self.config.remote, self.config.branch]
        )
        return result is not None

    def get_local_hash(self) -> Optional[str]:
        return self._run_git_command(["rev-parse", self.config.branch])

    def get_remote_hash(self) -> Optional[str]:
        return self._run_git_command(
            ["rev-parse", f"{self.config.remote}/{self.config.branch}"]
        )

    def get_current_branch(self) -> Optional[str]:
        return self._run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])

    def is_working_tree_clean(self) -> bool:
        """Vérifie qu'il n'y a pas de modifications locales non commitées."""
        result = self._run_git_command(["status", "--porcelain"])
        return result == ""

    def get_ahead_behind(self) -> Optional[tuple[int, int]]:
        result = self._run_git_command(
            [
                "rev-list",
                "--left-right",
                "--count",
                f"{self.config.branch}...{self.config.remote}/{self.config.branch}",
            ]
        )
        if result is None:
            return None
        try:
            ahead, behind = map(int, result.split())
            return ahead, behind
        except ValueError:
            return None

    def pull(self) -> tuple[bool, str]:
        """Effectue un pull (fast-forward uniquement, plus sûr pour l'automatisation)."""
        current_branch = self.get_current_branch()
        if current_branch != self.config.branch:
            return False, (
                f"Branche actuellement checkout ({current_branch}) "
                f"différente de la branche cible ({self.config.branch}), pull annulé"
            )

        result = self._run_git_command(
            ["merge", "--ff-only", f"{self.config.remote}/{self.config.branch}"]
        )
        if result is None:
            return False, "Échec du merge --ff-only"
        return True, "Pull effectué avec succès (fast-forward)"

    def check_status(self) -> dict:
        report = {
            "name": self.config.name,
            "path": str(self.path),
            "status": "unknown",
            "message": "",
            "pulled": False,
        }

        if not self.is_valid_repo():
            report["status"] = "error"
            report["message"] = "Dépôt invalide ou introuvable"
            return report

        if not self.fetch():
            report["status"] = "error"
            report["message"] = "Échec du fetch"
            return report

        local_hash = self.get_local_hash()
        remote_hash = self.get_remote_hash()

        if local_hash is None or remote_hash is None:
            report["status"] = "error"
            report["message"] = "Impossible de récupérer les hash"
            return report

        if local_hash == remote_hash:
            report["status"] = "up_to_date"
            report["message"] = "À jour"
            return report

        ahead_behind = self.get_ahead_behind()
        if ahead_behind is None:
            report["status"] = "different"
            report["message"] = "Hash différents (détails indisponibles)"
            return report

        ahead, behind = ahead_behind

        if behind > 0 and ahead > 0:
            report["status"] = "diverged"
            report["message"] = (
                f"Diverge : {ahead} commit(s) en avance, {behind} en retard"
            )
        elif behind > 0:
            report["status"] = "behind"
            report["message"] = f"En retard de {behind} commit(s)"
        elif ahead > 0:
            report["status"] = "ahead"
            report["message"] = f"En avance de {ahead} commit(s)"

        # --- Logique auto-pull ---
        if self.config.auto_pull:
            can_pull = False
            reason = ""

            if report["status"] == "behind":
                can_pull = True
            elif report["status"] == "diverged" and self.config.allow_diverged_pull:
                can_pull = True
            elif report["status"] == "diverged":
                reason = "auto_pull activé mais diverged et allow_diverged_pull=false"

            if (
                can_pull
                and self.config.require_clean
                and not self.is_working_tree_clean()
            ):
                can_pull = False
                reason = "working tree non propre (modifications locales détectées)"

            if can_pull:
                success, pull_message = self.pull()
                report["pulled"] = success
                report["message"] += f" | Auto-pull: {pull_message}"
                if success:
                    report["status"] = "auto_pulled"
            elif reason:
                report["message"] += f" | Auto-pull ignoré: {reason}"

        return report


class GitWatcherDaemon:
    """Daemon principal qui orchestre la surveillance de tous les dépôts."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.running = True
        self.config = {}
        self.repos: list[RepoConfig] = []
        self.logger = logging.getLogger("git_watcher")

        self._load_config()
        self._setup_logging()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_reload)

    def _load_config(self):
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        self.check_interval = self.config.get("check_interval", 300)
        self.repos = [
            RepoConfig(
                name=r["name"],
                path=r["path"],
                remote=r.get("remote", "origin"),
                branch=r.get("branch", "main"),
                auto_pull=r.get("auto_pull", False),
                require_clean=r.get("require_clean", True),
                allow_diverged_pull=r.get("allow_diverged_pull", False),
            )
            for r in self.config.get("repositories", [])
        ]

    def _setup_logging(self):
        log_file = self.config.get("log_file")
        log_level = self.config.get("log_level", "INFO").upper()

        handlers = [logging.StreamHandler(sys.stdout)]
        if log_file:
            handlers.append(logging.FileHandler(log_file))

        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=handlers,
            force=True,
        )
        self.logger = logging.getLogger("git_watcher")

    def _handle_signal(self, signum, frame):
        self.logger.info(f"Signal {signum} reçu, arrêt en cours...")
        self.running = False

    def _handle_reload(self, signum, frame):
        self.logger.info("Signal SIGHUP reçu, rechargement de la configuration...")
        try:
            self._load_config()
            self._setup_logging()
            self.logger.info("Configuration rechargée avec succès.")
        except Exception as e:
            self.logger.error(f"Erreur lors du rechargement de la config: {e}")

    def check_all_repos(self):
        if not self.repos:
            self.logger.warning("Aucun dépôt configuré.")
            return

        self.logger.info(f"Début de la vérification de {len(self.repos)} dépôt(s)")

        for repo_config in self.repos:
            checker = GitRepoChecker(repo_config, self.logger)
            report = checker.check_status()
            self._handle_report(report)

        self.logger.info("Vérification terminée")

    def _handle_report(self, report: dict):
        name = report["name"]
        status = report["status"]
        message = report["message"]

        icons = {
            "up_to_date": "✓",
            "behind": "⚠",
            "ahead": "↑",
            "diverged": "⚡",
            "auto_pulled": "⬇",
            "error": "✗",
        }
        icon = icons.get(status, "?")

        if status in ("error", "diverged"):
            self.logger.warning(f"{icon} [{name}] {message}")
        elif status == "behind" and not report.get("pulled"):
            self.logger.warning(f"{icon} [{name}] {message}")
        else:
            self.logger.info(f"{icon} [{name}] {message}")

    def run(self):
        self.logger.info(f"Démarrage du daemon git-watcher (PID {os.getpid()})")
        self.logger.info(f"Intervalle de vérification : {self.check_interval}s")

        while self.running:
            try:
                self.check_all_repos()
            except Exception as e:
                self.logger.exception(f"Erreur inattendue pendant la vérification: {e}")

            for _ in range(int(self.check_interval)):
                if not self.running:
                    break
                time.sleep(1)

        self.logger.info("Daemon arrêté proprement.")


def main():
    parser = argparse.ArgumentParser(description="Daemon de surveillance de dépôts Git")
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Chemin vers le fichier de configuration",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Effectue une seule vérification puis quitte",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Lance le processus en vrai daemon Unix (double-fork)",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Arrête le daemon en cours (via pidfile)",
    )
    parser.add_argument(
        "--pidfile",
        default=None,
        help="Chemin du pidfile (surcharge celui du config.yaml)",
    )
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"Erreur: fichier de config introuvable: {args.config}", file=sys.stderr)
        sys.exit(1)

    # Charge juste pour récupérer le pidfile si besoin (avant démonisation)
    with open(args.config, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)
    pidfile = args.pidfile or raw_config.get("pidfile", "/tmp/git_watcher.pid")

    if args.stop:
        daemonizer = Daemonizer(pidfile=pidfile)
        daemonizer.stop()
        sys.exit(0)

    if args.daemon:
        log_file = raw_config.get("log_file", "/tmp/git_watcher.log")
        daemonizer = Daemonizer(
            pidfile=pidfile,
            stdout=log_file,
            stderr=log_file,
        )

        existing_pid = daemonizer.get_running_pid()
        if existing_pid:
            print(f"Le daemon tourne déjà (PID {existing_pid}).", file=sys.stderr)
            sys.exit(1)

        daemonizer.daemonize()
        # À partir d'ici, on est dans le processus démonisé (détaché du terminal)

    daemon = GitWatcherDaemon(args.config)

    if args.once:
        daemon.check_all_repos()
    else:
        daemon.run()


if __name__ == "__main__":
    main()
