# AI Cluster Doctor - Agent image.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent_core.py .
CMD ["python", "agent_core.py"]
