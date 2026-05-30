from passlib.context import CryptContext
pwd = 'a' * 200
ctx = CryptContext(schemes=['bcrypt'], deprecated='auto')
try:
    h = ctx.hash(pwd)
    print('OK', len(h))
except Exception as e:
    import traceback
    traceback.print_exc()
    print('EXC:', str(e))
