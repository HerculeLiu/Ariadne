from __future__ import annotations

from ariadne.api.facade import AriadneAPI


def bootstrap() -> AriadneAPI:
    return AriadneAPI()


if __name__ == "__main__":
    api = bootstrap()
    print(api.health_live())
