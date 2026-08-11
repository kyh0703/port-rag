from sqlalchemy.engine import make_url


def normalize_asyncpg_url(database_url: str) -> str:
    url = make_url(database_url)
    sslmode = url.query.get("sslmode")
    if sslmode is None:
        return database_url

    query = dict(url.query)
    query.pop("sslmode")
    query["ssl"] = sslmode
    return url.set(query=query).render_as_string(hide_password=False)
