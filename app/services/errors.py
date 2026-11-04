class ServiceError(Exception):
    """利用者に見せてよいエラー（入力不備・状態不整合など）。"""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PermissionDenied(ServiceError):
    status_code = 403


class NotFound(ServiceError):
    status_code = 404


class VersionConflict(ServiceError):
    """楽観的ロックの不一致（F6-14）。"""

    status_code = 409

    def __init__(self, message: str = "他のユーザーが先に更新しました。最新の内容を確認してから再度保存してください。"):
        super().__init__(message)


def check_version(obj, submitted: int | str | None) -> None:
    if submitted is None or submitted == "":
        return
    if int(submitted) != obj.version:
        raise VersionConflict()


def bump(obj) -> None:
    obj.version = (obj.version or 0) + 1
