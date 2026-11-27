FROM python:3.12-slim
WORKDIR /app
RUN python -c 'import ssl; from pathlib import Path; from urllib.request import urlopen;     base = "https://gu-st.ru/content/lending/russian_trusted_";     with_certs = [urlopen(base + name + "_ca_pem.crt", timeout=30).read().strip() for name in ("root", "sub")];     path = Path("/app/tochka-ca.pem");     path.write_bytes(b"\n".join(with_certs) + b"\n");     ssl.create_default_context().load_verify_locations(cafile=str(path))'
COPY server.py .
COPY static static
CMD ["python","-u","server.py"]
