# sish v2 tunnel

VPS에서 `./run_server.sh`로 서버를 켜두고, 서비스 Docker가 연결하면서 자신의 도메인을 등록하는 구성이다. 기존 `tunnel/`의 Caddy·Python 서버 파일은 유지하며, 이 폴더에서 별도로 실행한다.

```text
사용자 HTTPS 요청 → VPS sish :443 → 도메인에 해당하는 SSH 터널 → 서비스 Docker 앱
                                      ↑
                      Docker가 sish :2222로 연결하면서 도메인 등록
```

서비스마다 VPS 포트를 배정하거나 원격 `tunnel.py`를 실행하지 않는다. 새 서비스가 연결할 때 VPS의 도메인 목록을 수정하거나 sish를 재시작할 필요도 없다.

## VPS에서 시작하기

VPS에 Docker, Docker Compose v2 이상, Python 3가 필요하다. 기존 Python 터널 서버를 실행하던 VPS라면 Python 3는 그대로 사용한다.

이 폴더를 VPS의 `/home/linuxuser/tunnel/v2_tunnel`에 복사한 뒤 설정한다.

```sh
cd /home/linuxuser/tunnel/v2_tunnel
cp .env.example .env
chmod 600 .env
nano .env
```

`.env`의 `SSH_PASSWORD`에는 **서비스 Docker에 전달하는 기존 `SSH_PASSWORD`와 같은 값**을 넣는다. Linux 계정 비밀번호를 sish가 자동으로 읽는 방식은 아니다. 예를 들어 공백이나 `$`가 들어간 값을 그대로 전달하려면 `.env`에서 작은따옴표로 감싼다.

```dotenv
SSH_PASSWORD='서비스 Docker와 같은 비밀번호'
SISH_DOMAIN=ourmemories.kr
SISH_BIND_ADDRESS=0.0.0.0
SISH_HTTP_PORT=80
SISH_HTTPS_PORT=443
SISH_SSH_PORT=2222
SISH_CERTIFICATE_EMAIL=
```

셸에 이미 `SSH_PASSWORD`를 export했다면 그 값이 `.env`보다 우선한다. 두 값이 같은지 확인하거나 `unset SSH_PASSWORD` 후 실행한다. `SISH_CERTIFICATE_EMAIL`은 선택 사항이다.

설정 문법을 먼저 확인한다. 이 명령은 서버를 시작하지 않는다.

```sh
./run_server.sh check
```

기본 `80/443`은 기존 Caddy와 공유할 수 없으므로, 실제 도메인으로 시험할 때는 기존 터널 서버를 종료한 다음 새 서버를 시작한다. 이 전환 중에는 서비스 연결이 끊기는 시간이 발생한다.

```sh
../run_server.sh stop
./run_server.sh
./run_server.sh status
```

VPS 방화벽에서 외부 HTTP/HTTPS용 `80/443`과 서비스 Docker의 연결용 TCP `2222`가 열려 있어야 한다. 서비스 도메인의 DNS는 이 VPS를 가리켜야 한다. 기존 도메인을 그대로 사용한다면 DNS를 바꿀 필요는 없다.

`./run_server.sh`는 컨테이너를 백그라운드로 실행하고 HTTP 응답, HTTPS 포트 연결, SSH 배너를 확인한 뒤 반환한다. 이 확인은 앱의 응답이나 HTTPS 인증서 발급 완료까지 보장하지 않는다.

## ourmemories Docker에서 연결하기

`/Users/jb/ourmemories/server/Dockerfile`의 기존 `SSH_PORT=9015`와 `CMD`는 주석으로 보관했고, 새 `CMD`를 적용했다. 앱 실행과 기존 설치 과정은 유지한다.

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| `SERVER_ENDPOINT` | `api.ourmemories.kr` | 등록할 도메인 |
| `PORT` | `8000` | Docker 내부 앱 포트 |
| `TUNNEL_HOST` | `158.247.248.94` | sish VPS 주소 |
| `TUNNEL_SSH_PORT` | `2222` | sish SSH 포트 |
| `TUNNEL_USER` | `linuxuser` | SSH 연결 사용자 이름 |
| `SSH_PASSWORD` | 기존 build arg 또는 runtime env | sish 연결 인증값 |

새 연결 요청은 다음과 같다.

```sh
ssh -N -T -p 2222 \
    -R api.ourmemories.kr:80:localhost:8000 \
    linuxuser@158.247.248.94
```

실제 Dockerfile에서는 기존 `sshpass`와 `autossh`로 이 연결을 시작하고 유지한다. `-R`의 `:80:`은 sish에 HTTP 도메인 등록을 요청하는 값이다. 앱은 계속 `8000`으로 연결되고, 외부 HTTPS는 VPS의 `443`에서 처리한다. 모든 서비스는 같은 SSH 포트에 접속해 각자의 도메인을 등록한다.

Dockerfile 변경은 **기존 컨테이너를 재시작하는 것만으로 반영되지 않는다. 이미지를 다시 빌드하고 컨테이너를 교체해야 한다.** 기존 배포 명령을 그대로 쓸 수 있다.

```sh
cd /Users/jb/ourmemories/server
# 기존 방식으로 SSH_PASSWORD와 DOPPLER_TOKEN을 설정한 뒤 실행
bun run deploy:docker
```

위 프로젝트 명령은 이미지를 빌드한 후 기존 `ourmemories-server` 컨테이너를 교체한다. 이 문서 작성 과정에서는 실행하지 않았다.

서비스가 연결한 다음 실제 도메인을 확인한다. 인증서는 첫 HTTPS 요청에서 발급되므로 처음 요청에 시간이 더 걸릴 수 있다.

```sh
curl -I http://api.ourmemories.kr/
curl -i https://api.ourmemories.kr/
```

첫 번째 요청은 HTTPS로 이동해야 한다. 두 번째는 해당 앱의 응답을 반환해야 한다. 앱의 `/` 경로가 404를 반환하는 경우에는 실제 정상 동작하는 API 경로를 사용한다. 등록되지 않은 도메인은 앱으로 전달되지 않는다.

## 실행과 상태 확인

```sh
./run_server.sh             # 시작, 이미 실행 중이면 유지
./run_server.sh status      # 컨테이너 상태
./run_server.sh logs        # 최근 로그 및 이후 로그, Ctrl+C로 보기 종료
./run_server.sh restart     # 컨테이너 재생성, SSH 연결은 재연결 필요
./run_server.sh stop        # 정지, 인증서와 서버 키는 보관
./run_server.sh check       # Compose 설정 확인
./run_server.sh --foreground
```

인증서와 SSH 서버 키는 이 폴더의 `.runtime/ssl`, `.runtime/keys`에 저장한다. `.runtime/pubkeys`는 공개키 인증용이다. `.env`와 `.runtime/`은 Git에서 제외한다. 컨테이너 로그는 파일당 10MB, 최대 5개로 제한한다.

같은 도메인을 두 서비스가 동시에 등록하면 두 번째 등록을 실패시킨다. 기존 서비스의 등록은 유지한다. 연결 종료가 확인되면 해당 도메인을 정리하고, `autossh`가 재연결하면 다시 등록한다.

서버 프로세스 종료에는 Docker의 `unless-stopped` 재시작 정책을 사용한다. **기존 Python supervisor의 지속적인 응답 검사·hang 복구 watchdog은 이 버전에 옮기지 않았다.** `status`도 컨테이너 상태를 표시하며, 모든 서비스가 정상 응답하는지 검사하는 명령은 아니다.

## 기존 Caddy와 포트를 나눠 시험하기

기존 서버를 켜둔 채 포트 바인딩과 새 SSH 연결을 시험하려면 `.env`에서 `SISH_HTTP_PORT=8080`, `SISH_HTTPS_PORT=8443`, `SISH_SSH_PORT=2223`처럼 바꿀 수 있다. 컨테이너 실행 시 `TUNNEL_SSH_PORT=2223`도 전달해야 한다. 새 이미지에 다른 VPS를 사용할 때는 `TUNNEL_HOST`를 전달한다.

```sh
docker run -d --name ourmemories-server-sish-test \
    -e TUNNEL_HOST=158.247.248.94 \
    -e TUNNEL_SSH_PORT=2223 \
    ourmemories-server
```

이 경우 기존 Caddy는 일반 도메인 요청을 계속 받으며, sish HTTPS는 `https://api.ourmemories.kr:8443/`로 접근한다. **포트만 나누면 Let's Encrypt 인증서 발급까지 그대로 시험할 수 있는 것은 아니다.** 인증기관의 검증 요청은 표준 `80/443`에 도달해야 하므로, 병행 시험에서 HTTPS까지 확인하려면 sish에 해당 도메인의 인증서와 키를 따로 준비해야 한다. 인증서와 같은 basename의 `.crt`·`.key` 쌍을 `.runtime/ssl/`에 둔다. 실제 자동 발급 시험은 위의 기본 `80/443` 전환 절차로 진행한다.

## 기존 구성으로 되돌리기

1. 이 폴더에서 `./run_server.sh stop`으로 sish를 정지한다.
2. ourmemories Dockerfile의 새 `CMD`와 `TUNNEL_*` 설정을 주석 처리하고, 보관한 기존 `ENV SSH_PORT=9015`와 기존 `CMD`의 주석을 해제한다.
3. 기존 서버를 `../run_server.sh`로 다시 시작한다.
4. 기존 방식으로 ourmemories 이미지를 다시 빌드하고 컨테이너를 교체한다.

기존 Caddy 설정과 `.runtime` 데이터는 이 구현에서 수정하지 않는다. 새 sish 서버는 그 인증서를 자동으로 가져오지 않으며 자기 저장소를 사용한다.

## 격리된 통합 테스트

`tests/test_sish_integration.py`는 실제 `compose.yml`의 옵션을 읽고, 공식 sish 바이너리와 로컬 SSH로 도메인 등록·전달을 시험한다. 임시 인증서와 무작위 비밀번호를 사용하며, 임의의 loopback 포트에서만 실행한다. VPS 접속이나 실제 인증서 발급은 하지 않는다.

공식 릴리스에서 현재 OS에 맞는 **v2.23.0 바이너리**를 받아 압축을 풀고 실행 경로를 지정한다. Docker Compose는 설정 해석에만 사용하므로 Docker daemon은 필요 없다. 독립 실행형 Compose 바이너리도 지정할 수 있다.

```sh
cd /Users/jb/tunnel/v2_tunnel
SISH_TEST_BIN=/path/to/sish \
    python3 -m unittest discover -s tests -v

# Docker가 없는 환경에서 독립 실행형 Compose 사용
SISH_TEST_BIN=/path/to/sish \
    SISH_TEST_COMPOSE_BIN=/path/to/docker-compose \
    python3 -m unittest discover -s tests -v
```

검증한 항목은 다음과 같다.

- 서로 다른 루트에 속한 도메인과 루트 도메인의 앱 선택, Host·HTTPS 헤더·경로 보존, HTTP→HTTPS 리다이렉트
- 중복 도메인 등록 거절과 기존 서비스 유지
- 연결 종료 후 도메인 정리와 재연결 등록
- 잘못된 비밀번호의 연결 거절
- 10MiB 업로드 내용 보존과 6초 걸리는 응답
- 다른 서비스의 등록·종료 중 6초 유휴 WebSocket 유지
- sish 서버 재시작 뒤 클라이언트의 재등록

이 Mac에서 위 **7개 테스트가 통과**했고, Compose 설정과 Dockerfile의 실행 명령을 검증했다. Mac에 Docker가 없어 실제 Docker 이미지 빌드·실행, `autossh`의 자동 재접속, 공인 인증서 발급은 실행하지 않았다. 실제 서비스의 처리 성능이 기존보다 좋은지는 측정하지 않았다.

참고: [sish v2.23.0](https://github.com/antoniomika/sish/releases/tag/v2.23.0), [HTTP 포워딩](https://docs.ssi.sh/forwarding-types#http), [CLI 옵션](https://docs.ssi.sh/cli).
