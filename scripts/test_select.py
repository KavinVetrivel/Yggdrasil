import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import auth_store

student_id = 'test_student_42'
email = 'test42@example.local'
conn = auth_store.get_connection()
cur = conn.cursor()
print('running select with params:', repr(student_id), repr(email))
try:
    cur.execute("SELECT 1 FROM users WHERE student_id = %s OR (%s IS NOT NULL AND email = %s)", (student_id, email, email))
    print('ok, fetched', cur.fetchone())
except Exception as e:
    print('select failed:', type(e).__name__, e)
    import traceback; traceback.print_exc()
finally:
    cur.close(); conn.close()
