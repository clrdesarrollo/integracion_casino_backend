pipeline {
    agent any

    options {
        buildDiscarder logRotator(artifactNumToKeepStr: '10', numToKeepStr: '10')
        disableConcurrentBuilds()
        timestamps()
    }

    environment {
        DOCKER_REGISTRY = 'registry.clrobotics.cl'
        IMAGE_NAME      = 'casino-backend'
        IMAGE_TAG       = "1.0.${BUILD_NUMBER}"
        DOCKERFILE      = './docker/web/Dockerfile'

        // cache tag para buildx (en el mismo registry)
        BUILDCACHE_REF  = "${DOCKER_REGISTRY}/${IMAGE_NAME}:buildcache"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout([$class: 'GitSCM',
                    branches: [[name: '*/main']],
                    userRemoteConfigs: [[
                        credentialsId: 'github-clr-jenkins',
                        url: 'https://github.com/clrdesarrollo/integracion_casino_backend.git'
                    ]]
                ])
                sh 'ls -lart ./*'
            }
        }

        stage('Tests') {
            environment {
                CI_NET     = "casinoci-${BUILD_NUMBER}"
                CI_DB      = "casinoci-db-${BUILD_NUMBER}"
                CI_REDIS   = "casinoci-redis-${BUILD_NUMBER}"
                CI_RUNNER  = "casinoci-tests-${BUILD_NUMBER}"
                TEST_IMAGE = "casino-backend-test:${BUILD_NUMBER}"

                // Credenciales de la base efímera: viven y mueren con el build.
                TEST_DB_NAME = 'casinotest'
                TEST_DB_USER = 'casinotest'
                TEST_DB_PASS = 'casinotest'
            }
            steps {
                sh '''
                    set -e

                    docker network create "${CI_NET}"

                    # Misma versión que docker/postgresql/Dockerfile.
                    docker run -d --name "${CI_DB}" --network "${CI_NET}" --network-alias pgdb \
                        -e POSTGRES_DB="${TEST_DB_NAME}" \
                        -e POSTGRES_USER="${TEST_DB_USER}" \
                        -e POSTGRES_PASSWORD="${TEST_DB_PASS}" \
                        postgres:16-alpine

                    # El channel layer apunta al host "redis" por defecto (REDIS_HOST), de ahí el alias.
                    docker run -d --name "${CI_REDIS}" --network "${CI_NET}" --network-alias redis \
                        redis:7-alpine

                    for i in $(seq 1 30); do
                        if docker exec "${CI_DB}" pg_isready -U "${TEST_DB_USER}" -d "${TEST_DB_NAME}" >/dev/null 2>&1; then
                            break
                        fi
                        if [ "$i" = "30" ]; then
                            echo "Postgres no aceptó conexiones tras 60s"
                            docker logs "${CI_DB}" | tail -n 30
                            exit 1
                        fi
                        sleep 2
                    done

                    # Misma imagen que se publica, para probar lo que realmente se despliega.
                    docker build -f "${DOCKERFILE}" -t "${TEST_IMAGE}" .

                    rm -rf reports && mkdir -p reports

                    # El reporte se deja en /tmp del contenedor y se extrae después con
                    # docker cp, así no dependemos de permisos sobre un volumen montado.
                    set +e
                    docker run --name "${CI_RUNNER}" --network "${CI_NET}" \
                        -e POSTGRES_DB="${TEST_DB_NAME}" \
                        -e POSTGRES_USER="${TEST_DB_USER}" \
                        -e POSTGRES_PASSWORD="${TEST_DB_PASS}" \
                        -e HOST_DB=pgdb \
                        -e PORT_DB=5432 \
                        -e REDIS_HOST=redis \
                        -e REDIS_PORT=6379 \
                        -e DEBUG=0 \
                        -e ALLOWED_HOSTS='*' \
                        -e DJANGO_SECRET_KEY=ci-build-secret-not-used-in-deploy \
                        "${TEST_IMAGE}" \
                        pytest backend/apps -p no:cacheprovider --junitxml=/tmp/junit.xml
                    TEST_RC=$?
                    set -e

                    docker cp "${CI_RUNNER}:/tmp/junit.xml" reports/junit.xml || true

                    exit ${TEST_RC}
                '''
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'reports/junit.xml'
                    sh '''
                        docker rm -f "${CI_RUNNER}" "${CI_DB}" "${CI_REDIS}" >/dev/null 2>&1 || true
                        docker network rm "${CI_NET}" >/dev/null 2>&1 || true
                        docker image rm -f "${TEST_IMAGE}" >/dev/null 2>&1 || true
                    '''
                }
            }
        }

        stage('Preparing...') {
            steps {
                sh """
                    set -e
                    echo "${IMAGE_TAG}" > ./version
                    cat ./version

                    # Por si el entrypoint llega con finales de línea CRLF desde Windows.
                    dos2unix ./docker/web/entrypoint.sh || true
                    chmod +x ./docker/web/entrypoint.sh || true
                """
            }
        }

        stage('Login to Docker Registry') {
            steps {
                script {
                    withCredentials([usernamePassword(credentialsId: 'jenkins-clr-registry', usernameVariable: 'DOCKER_USER', passwordVariable: 'DOCKER_PASSWORD')]) {
                        sh '''
                            set -e
                            echo "${DOCKER_PASSWORD}" | docker login "${DOCKER_REGISTRY}" -u "${DOCKER_USER}" --password-stdin
                        '''
                    }
                }
            }
        }

        stage('Build & Push (single image + buildx cache)') {
            steps {
                sh """
                    set -e
                    export DOCKER_BUILDKIT=1

                    # Usa builder si existe, si no lo crea (sin fallar)
                    if docker buildx inspect casinobuilder >/dev/null 2>&1; then
                        docker buildx use casinobuilder
                    else
                        docker buildx create --name casinobuilder --driver docker-container --use
                    fi

                    docker buildx inspect --bootstrap

                    docker buildx build \\
                        --cache-from=type=registry,ref=${BUILDCACHE_REF} \\
                        --cache-to=type=registry,ref=${BUILDCACHE_REF},mode=max \\
                        -t ${DOCKER_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG} \\
                        -t ${DOCKER_REGISTRY}/${IMAGE_NAME}:latest \\
                        -f ${DOCKERFILE} \\
                        --push \\
                        .
                """
            }
        }

        // Sin etapa de despliegue: se despliega a mano con el tag publicado (IMAGE_TAG).

        stage('Cleanup (safe)') {
            steps {
                sh """
                    set -e
                    # Limpieza segura (NO borra todo el cache). Ajusta horas según tu gusto.
                    docker builder prune -f --filter 'until=168h' || true
                    docker image prune -f --filter 'until=168h' || true
                """
            }
        }
    }

    post {
        success {
            echo "Imagen publicada: ${DOCKER_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG} (y :latest)"
        }
        always {
            sh "docker logout ${DOCKER_REGISTRY} || true"
        }
    }
}
