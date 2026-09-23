"""
Démonisation Unix classique (double-fork) selon le modèle décrit
dans Stevens' Advanced Programming in the UNIX Environment.
"""

import atexit
import os
import sys
import signal


class Daemonizer:
    def __init__(
        self, pidfile: str, stdout="/dev/null", stderr="/dev/null", stdin="/dev/null"
    ):
        self.pidfile = pidfile
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = stdin

    def daemonize(self):
        # Premier fork : détache du terminal parent
        self._fork()

        # Devient leader de session, se détache du terminal contrôleur
        os.setsid()
        os.umask(0)

        # Deuxième fork : empêche de ré-acquérir un terminal contrôleur
        self._fork()

        # Redirige les flux standards
        sys.stdout.flush()
        sys.stderr.flush()

        with open(self.stdin, "rb", 0) as f:
            os.dup2(f.fileno(), sys.stdin.fileno())
        with open(self.stdout, "ab", 0) as f:
            os.dup2(f.fileno(), sys.stdout.fileno())
        with open(self.stderr, "ab", 0) as f:
            os.dup2(f.fileno(), sys.stderr.fileno())

        # Écrit le PID file et prévoit le nettoyage
        atexit.register(self._cleanup_pidfile)
        pid = str(os.getpid())
        with open(self.pidfile, "w") as f:
            f.write(pid + "\n")

    def _fork(self):
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)  # quitte le processus parent
        except OSError as e:
            sys.stderr.write(f"Échec du fork: {e}\n")
            sys.exit(1)

    def _cleanup_pidfile(self):
        try:
            os.remove(self.pidfile)
        except FileNotFoundError:
            pass

    def get_running_pid(self):
        """Retourne le PID du daemon en cours si le pidfile existe et est valide."""
        try:
            with open(self.pidfile, "r") as f:
                pid = int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return None

        # Vérifie que le processus existe réellement
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid

    def stop(self):
        pid = self.get_running_pid()
        if pid is None:
            print("Le daemon n'est pas en cours d'exécution.")
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            print(f"Erreur lors de l'arrêt: {e}")
