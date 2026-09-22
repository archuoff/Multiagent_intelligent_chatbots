from dataclasses import dataclass


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    groups: tuple[str, ...] = ()


def get_current_user() -> CurrentUser:
    # Temporary dev auth. Later replace with login/JWT.
    return CurrentUser(user_id="dev-user", groups=("dev",))