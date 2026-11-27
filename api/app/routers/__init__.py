from fastapi import Response, status


def status_for_create(response: Response, created: bool) -> None:
    """Downgrade the route's default 201 to 200 when `create_idempotent` replayed an
    existing row instead of inserting one.
    """
    if not created:
        response.status_code = status.HTTP_200_OK
