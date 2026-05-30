import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import auth_store

try:
    rec = auth_store.register_user(student_id='test_student_42', password='password123', email='test42@example.local', college_id='c1', regulation_id='r1')
    print('registered:', rec)
except Exception as e:
    print('register failed:', type(e).__name__, e)
