import requests

data = "".join(str(i) for i in range(1, 250000))
r = requests.post("http://0.0.0.0:8000/stream", data=data)
print(r.text)
