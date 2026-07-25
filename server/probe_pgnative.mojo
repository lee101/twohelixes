"""Confirms twoHelixes can consume the mojo-db package as a compiled artefact."""

from db.postgres.connection import ConnectParams, connect


def main() raises:
    var conn = connect(
        ConnectParams("127.0.0.1", 5432, "mojodb", "mojo_test_pw", "mojodb_test")
    )
    var r = conn.query("SELECT count(*)::int FROM metrics")
    print("mojo-db consumed from twohelixes; metrics rows =", r.rows[0].int(0))
    conn.close()
