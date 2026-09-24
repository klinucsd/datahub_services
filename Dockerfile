#
FROM python:3.9

ENV JAVA_HOME=/opt/java/openjdk
COPY --from=eclipse-temurin:17-jre $JAVA_HOME $JAVA_HOME
ENV PATH="${JAVA_HOME}/bin:${PATH}"

#
WORKDIR /code

#
COPY ./requirements.txt /code/requirements.txt

#
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Define environment variable
ENV WFR_VERSION=0.1
ENV WFR_DOCS_URL=/docs
ENV WFR_BASE_PATH=/v1

#
COPY ./fastApi /code/fastApi
COPY ./controller /code/controller
COPY ./views /code/views
COPY ./models /code/models
COPY ./routes /code/routes
COPY ./main.py /code/main.py
COPY ./server.py /code/server.py
COPY ./postgresql-42.6.0.jar /code/postgresql-42.6.0.jar


CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7000"]


