import psycopg2

conn = psycopg2.connect(host='localhost', port=5432, dbname='skillsync', user='skillsync', password='skillsync')
cur = conn.cursor()

# Check tables
cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;")
tables = cur.fetchall()
print('Tables:', [t[0] for t in tables])

# Count messages
cur.execute('SELECT COUNT(*) FROM message_records;')
print(f'Messages: {cur.fetchone()[0]}')

# Count by date
cur.execute('SELECT date(created_at) as day, COUNT(*) FROM message_records GROUP BY date(created_at) ORDER BY day;')
for row in cur.fetchall():
    print(f'  {row[0]}: {row[1]} messages')

# Check guilds
cur.execute('SELECT guild_id, name, scanned_at FROM guild_info;')
for row in cur.fetchall():
    print(f'Guild: {row[1]} ({row[0]}) scanned={row[2]}')

conn.close()