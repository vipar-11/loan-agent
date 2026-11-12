"""
Roles and permissions (role-based access control).

Code never checks a role directly: endpoints and pages require a *permission*, and each role is granted a fixed set of
permissions here. Adding a role, or letting an SSO provider map its groups onto the existing roles, therefore only
touches this module.

Segregation of duties: the Administrator operates the platform (monitoring, workflow runs, configuration, checkout
overrides) and can read applications for audit purposes, but holds no credit authority — only Loan Underwriters can
open a review dossier and record a determination. Only Loan Applicants submit applications.
"""
from enum import Enum
from typing import Dict, FrozenSet, Optional


class Role(str, Enum):
    APPLICANT = "APPLICANT"
    UNDERWRITER = "UNDERWRITER"
    ADMINISTRATOR = "ADMINISTRATOR"


class Permission(str, Enum):
    APPLICATION_SUBMIT = "application:submit"          # Applicant Portal: submit a loan application
    APPLICATION_READ_OWN = "application:read_own"      # My Applications: track the status of one's own submissions
    APPLICATION_READ = "application:read"              # queue / archive listings, detail, audit trail, stats
    APPLICATION_UNDERWRITE = "application:underwrite"  # review dossier, ratio what-ifs, checkout, determination
    CHECKOUT_OVERRIDE = "checkout:override"            # force-release another reviewer's checkout
    WORKFLOW_RUN = "workflow:run"                      # dispatch the underwriting workflow (batch or single file)
    SYSTEM_ADMINISTER = "system:administer"            # administrator console: monitoring, configuration, intake


ROLE_LABELS: Dict[Role, str] = {
    Role.APPLICANT: "Loan Applicant",
    Role.UNDERWRITER: "Loan Underwriter",
    Role.ADMINISTRATOR: "Administrator",
}

ROLE_PERMISSIONS: Dict[Role, FrozenSet[Permission]] = {
    Role.APPLICANT: frozenset({Permission.APPLICATION_SUBMIT, Permission.APPLICATION_READ_OWN}),
    Role.UNDERWRITER: frozenset({Permission.APPLICATION_READ, Permission.APPLICATION_UNDERWRITE}),
    Role.ADMINISTRATOR: frozenset({
        Permission.APPLICATION_READ,
        Permission.CHECKOUT_OVERRIDE,
        Permission.WORKFLOW_RUN,
        Permission.SYSTEM_ADMINISTER,
    }),
}

# Workbench pages: (path, navigation label, permission required to open it), in navigation order.
PAGES = (
    ("/portal", "👤 Applicant Portal", Permission.APPLICATION_SUBMIT),
    ("/status", "📋 My Applications", Permission.APPLICATION_READ_OWN),
    ("/desk", "🛡️ Underwriter Desk", Permission.APPLICATION_UNDERWRITE),
    ("/admin", "🛠️ Administrator", Permission.SYSTEM_ADMINISTER),
)


def parse_role(value: str) -> Role:
    """Accepts the role name in any case, or its label / a common alias (e.g. 'admin', 'Loan Underwriter')."""
    clean = " ".join(str(value or "").replace("_", " ").replace("-", " ").split()).upper()
    aliases = {
        "APPLICANT": Role.APPLICANT, "LOAN APPLICANT": Role.APPLICANT,
        "UNDERWRITER": Role.UNDERWRITER, "LOAN UNDERWRITER": Role.UNDERWRITER,
        "ADMINISTRATOR": Role.ADMINISTRATOR, "ADMIN": Role.ADMINISTRATOR,
    }
    if clean not in aliases:
        raise ValueError(f"Unknown role '{value}'. Expected one of: {', '.join(r.value for r in Role)}.")
    return aliases[clean]


def permissions_for(role: Role) -> FrozenSet[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def home_path_for(permissions) -> Optional[str]:
    """The first workbench page the holder of `permissions` may open (where to land after sign-in)."""
    for path, _label, required in PAGES:
        if required in permissions:
            return path
    return None
