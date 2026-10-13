"""Direct-only Nobitex sessions; other venues retain their own proxy policy."""
import requests


def direct_nobitex_session() -> requests.Session:
    session = requests.Session()
    # Ignore all proxy environment variables, including legacy Nobitex proxy
    # settings. TLS verification remains enabled. OS VPN routes are external.
    session.trust_env = False
    return session
