import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import auth_store

print('Connection kwargs:', auth_store._connection_kwargs())
with auth_store.get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name='users'")
        cols = cur.fetchall()
        print('users columns:', cols)
        try:
            cur.execute('SELECT column_name FROM information_schema.columns WHERE table_name=\'users\' ORDER BY ordinal_position')
            col_names = [r[0] for r in cur.fetchall()]
            print('column names:', col_names)
        except Exception as e:
            print('could not list column names:', type(e).__name__, e)
        try:
            cur.execute('SELECT * FROM users LIMIT 5')
            rows = cur.fetchall()
            print('users sample rows:', rows)
        except Exception as e:
            print('select users failed:', type(e).__name__, e)
print('done')
