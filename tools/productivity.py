"""
Productivity / Communication toolkits.

Each of these needs real credentials -- they're all no-ops until you
provide the relevant env vars / credential files, at which point
get_tools() picks them up automatically.

pip installs:
    pip install langchain-google-community google-api-python-client google-auth-oauthlib   # Gmail
    pip install langchain-community slack_sdk                                                # Slack
    pip install langchain-community pygithub                                                 # GitHub
    pip install langchain-community python-gitlab                                             # GitLab
    pip install langchain-community atlassian-python-api                                      # Jira
    pip install langchain-community O365                                                      # Office365
    pip install langchain-community                                                           # Zapier NLA
"""

from __future__ import annotations

import os

from ._env import require_env


def get_tools() -> list:
    tools = []

    # --- Gmail ---
    # Needs a Google OAuth credentials.json at GMAIL_CREDENTIALS_PATH
    # (first run opens a browser to authorize; token is cached alongside it).
    try:
        creds_path = require_env("GMAIL_CREDENTIALS_PATH")
        from langchain_google_community import GmailToolkit
        from langchain_google_community.gmail.utils import (
            build_resource_service,
            get_gmail_credentials,
        )

        credentials = get_gmail_credentials(
            token_file=os.environ.get("GMAIL_TOKEN_PATH", "token.json"),
            client_secrets_file=creds_path,
            scopes=["https://mail.google.com/"],
        )
        api_resource = build_resource_service(credentials=credentials)
        toolkit = GmailToolkit(api_resource=api_resource)
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- Slack ---
    try:
        require_env("SLACK_USER_TOKEN")
        from langchain_community.agent_toolkits import SlackToolkit

        toolkit = SlackToolkit()
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- GitHub ---
    try:
        require_env("GITHUB_APP_ID")
        require_env("GITHUB_REPOSITORY")
        from langchain_community.agent_toolkits.github.toolkit import GitHubToolkit
        from langchain_community.utilities.github import GitHubAPIWrapper

        wrapper = GitHubAPIWrapper()
        toolkit = GitHubToolkit.from_github_api_wrapper(wrapper)
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- GitLab ---
    try:
        require_env("GITLAB_PERSONAL_ACCESS_TOKEN")
        require_env("GITLAB_REPOSITORY")
        from langchain_community.agent_toolkits.gitlab.toolkit import GitLabToolkit
        from langchain_community.utilities.gitlab import GitLabAPIWrapper

        wrapper = GitLabAPIWrapper()
        toolkit = GitLabToolkit.from_gitlab_api_wrapper(wrapper)
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- Jira ---
    try:
        require_env("JIRA_API_TOKEN")
        require_env("JIRA_USERNAME")
        require_env("JIRA_INSTANCE_URL")
        from langchain_community.agent_toolkits.jira.toolkit import JiraToolkit
        from langchain_community.utilities.jira import JiraAPIWrapper

        wrapper = JiraAPIWrapper()
        toolkit = JiraToolkit.from_jira_api_wrapper(wrapper)
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- Office365 (Word/Excel/PowerPoint/Outlook via O365) ---
    try:
        require_env("O365_CLIENT_ID")
        require_env("O365_CLIENT_SECRET")
        from langchain_community.agent_toolkits import O365Toolkit

        toolkit = O365Toolkit()
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    # --- Zapier Natural Language Actions ---
    try:
        require_env("ZAPIER_NLA_API_KEY")
        from langchain_community.agent_toolkits import ZapierToolkit
        from langchain_community.utilities.zapier import ZapierNLAWrapper

        wrapper = ZapierNLAWrapper()
        toolkit = ZapierToolkit.from_zapier_nla_wrapper(wrapper)
        tools.extend(toolkit.get_tools())
    except Exception:
        pass

    return tools
