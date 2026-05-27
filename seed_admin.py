"""
Crea el primer usuario administrador.
Uso: python seed_admin.py
"""
import sqlite3, getpass
from werkzeug.security import generate_password_hash

DB = 'dashboard.db'

def init_db():
    conn = sqlite3.connect(DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'viewer',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def seed(username, password, email='', role='admin'):
    init_db()
    try:
        conn = sqlite3.connect(DB)
        conn.execute('INSERT INTO users (username, email, password_hash, role) VALUES (?,?,?,?)',
                     (username, email, generate_password_hash(password, method='pbkdf2:sha256'), role))
        conn.commit()
        conn.close()
        print(f'✓ Usuario "{username}" creado como {role}.')
    except sqlite3.IntegrityError:
        print(f'El usuario "{username}" ya existe.')

def main():
    init_db()
    print('=== Crear usuario administrador ===')
    username = input('Username: ').strip()
    email    = input('Email (opcional): ').strip()
    password = getpass.getpass('Contraseña: ')
    confirm  = getpass.getpass('Confirmar contraseña: ')
    if password != confirm:
        print('ERROR: Las contraseñas no coinciden.')
        return
    seed(username, password, email)
    print('  Ahora ejecuta: python app.py')

if __name__ == '__main__':
    main()
