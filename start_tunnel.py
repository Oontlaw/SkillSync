from pyngrok import ngrok
import time, atexit

tunnel = ngrok.connect(5000, 'http')
print(f'TUNNEL_URL={tunnel.public_url}')

@atexit.register
def cleanup():
    ngrok.disconnect(tunnel.public_url)

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
