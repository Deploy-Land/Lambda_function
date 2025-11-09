import boto3, time, os, json, http.client, urllib.request, urllib.parse, urllib.error, base64

# --- Webhook URL 환경 변수 ---
DISCORD_URL = os.environ.get('DISCORD_WEBHOOK_URL')

# --- Beanstalk 환경 변수 ---
BEANSTALK_ENV_ID = os.environ.get('BEANSTALK_ENV_ID')  # 환경 ID 우선
BEANSTALK_ENV_NAME = os.environ.get('BEANSTALK_ENV_NAME')  # 환경 이름 대체
CHECK_URL = os.environ.get('CHECK_URL')
MAX_WAIT = int(os.environ.get('MAX_WAIT', 60))
INTERVAL = int(os.environ.get('INTERVAL', 30))

# 리전 명시
beanstalk = boto3.client('elasticbeanstalk', region_name='ap-northeast-2')


def lambda_handler(event, context):
    print(f"📥 Received event: {json.dumps(event)}")

    # HTTP API v2 (API Gateway HTTP API) 요청 처리
    # HTTP API v2 이벤트는 top-level에 'requestContext'->'http' 키를 가집니다.
    if isinstance(event, dict) and event.get("requestContext") and event["requestContext"].get("http"):
        print("🌐 Handling HTTP API request")
        return handle_http_api_event(event)

    # DynamoDB Streams 이벤트 처리
    if 'Records' in event:
        print("📦 Processing DynamoDB Stream records...")
        for record in event['Records']:
            event_name = record['eventName']
            print(f"🔄 Event: {event_name}")
            
            # INSERT와 MODIFY 모두 처리 (재배포 포함)
            if event_name in ['INSERT', 'MODIFY']:
                new_image = record['dynamodb'].get('NewImage', {})
                pipeline_data = parse_dynamodb_item(new_image)
                
                # 검증 로직 실행
                result = process_pipeline_validation(pipeline_data)
                if result:
                    return result
            else:
                print(f"⏭️ Skipping {event_name} event")
        
        return {"statusCode": 200, "message": "Processed all records"}
    
    # 테스트 이벤트 처리
    else:
        pipeline_data = parse_pipeline_event(event)
        return process_pipeline_validation(pipeline_data)

def handle_http_api_event(event):
    """
    API Gateway HTTP API (v2) 이벤트 처리
    """
    print("🌐 Handling HTTP API request for URL lookup...")
    
    try:
        # Beanstalk URL 가져오기
        check_url = get_auto_check_url()
        
        if not check_url:
            print("❌ Failed to get Beanstalk URL.")
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "Failed to retrieve Beanstalk environment URL."})
            }
            
        print(f"✅ Successfully retrieved URL: {check_url}")
        
        # 조회한 URL을 JSON으로 즉시 반환
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "message": "Beanstalk environment URL retrieved.",
                "beanstalkUrl": check_url
            })
        }
    except Exception as e:
        print(f"Error in handle_http_api_event: {e}")
        return {"statusCode": 500, "body": json.dumps({"message": "Internal error", "error": str(e)})}

def process_pipeline_validation(pipeline_data):
    """파이프라인 검증 로직 - 실제 헬스체크 수행"""
    if not pipeline_data:
        print("⚠️ Could not parse pipeline data from event")
        return {"statusCode": 400, "message": "Invalid event format"}
    
    pipeline_id = pipeline_data.get('pipelineID')
    current_stage = pipeline_data.get('currentStage')
    status = pipeline_data.get('status')
    log_url = pipeline_data.get('logUrl')
    
    print(f"📦 Pipeline ID: {pipeline_id}")
    print(f"📊 Current Stage: {current_stage}")
    print(f"✅ Status: {status}")
    
    # Deploy 스테이지이면서 성공한 경우만 검증
    if current_stage != 'Deploy':
        print(f"⏭️ Skipping - not Deploy stage (current: {current_stage})")
        return {"statusCode": 200, "message": "Skipped - not Deploy stage"}
    
    if status != 'SUCCEEDED':
        print(f"⏭️ Skipping - status is {status}")
        return {"statusCode": 200, "message": f"Skipped - status is {status}"}
    
    print(f"✅ Deploy stage succeeded - starting validation")
    
    # 환경 정보 가져오기
    env_identifier = BEANSTALK_ENV_ID or BEANSTALK_ENV_NAME
    if not env_identifier:
        error_msg = "Neither BEANSTALK_ENV_ID nor BEANSTALK_ENV_NAME is set in environment variables"
        print(f"❌ {error_msg}")
        send_discord_notification(f"⚠️ **[Config Error]** {error_msg}")
        raise ValueError(error_msg)
    
    print(f"🚀 Starting Beanstalk validation for environment: {env_identifier}")

    # --- CHECK_URL 자동 결정 또는 환경 변수 사용 ---
    global CHECK_URL
    if not CHECK_URL:
        CHECK_URL = get_auto_check_url()
        if not CHECK_URL:
            error_msg = f"Failed to auto-detect CHECK_URL"
            print(f"❌ {error_msg}")
            send_discord_notification(
                f"⚠️ **[Config Error]** {error_msg}\n"
                f"Please set CHECK_URL in environment variables."
            )
            raise ValueError(error_msg)
        print(f"✅ Auto-detected CHECK_URL: {CHECK_URL}")
    else:
        print(f"✅ Using configured CHECK_URL: {CHECK_URL}")

    # --- 배포 직후 대기 ---
    print(f"⏳ Waiting 30 seconds for environment to stabilize...")
    time.sleep(30)

    # --- 상태 검증 루프 ---
    start_time = time.time()
    success = False
    reason = ""

    while time.time() - start_time < MAX_WAIT:
        try:
            # Beanstalk 헬스 상태 확인
            status_response = describe_environment_health()
            if status_response:
                color = status_response.get('Color', 'Unknown')
                health = status_response.get('HealthStatus', 'Unknown')
                print(f"Beanstalk Health: Color={color}, Status={health}")
            else:
                color = 'green'  # 헬스 체크 실패 시 HTTP로만 확인

            # HTTP 응답 확인
            if color.lower() == 'green' or not status_response:
                try:
                    req = urllib.request.Request(CHECK_URL, method='GET')
                    with urllib.request.urlopen(req, timeout=10) as response:
                        status_code = response.getcode()
                        if status_code == 200:
                            success = True
                            break
                        else:
                            reason = f"HTTP {status_code} from {CHECK_URL}"
                except urllib.error.HTTPError as e:
                    reason = f"HTTP {e.code} from {CHECK_URL}"
                except urllib.error.URLError as e:
                    reason = f"URL error: {str(e.reason)}"
                except Exception as e:
                    reason = f"HTTP request failed: {str(e)}"
            else:
                reason = f"Beanstalk not healthy: {color}/{health}"

        except Exception as e:
            reason = f"Error checking Beanstalk: {str(e)}"

        time.sleep(INTERVAL)

    # --- 결과 보고 ---
    env_display = BEANSTALK_ENV_NAME or BEANSTALK_ENV_ID
    
    if success:
        message = (
            f"✅ **[Deploy Success]** 배포 검증 완료!\n"
            f"**환경:** `{env_display}`\n"
            f"**Pipeline ID:** `{pipeline_id}`\n"
            f"서비스가 정상적으로 동작 중입니다. 🎉"
        )
        send_discord_notification(message)
        print("✅ Validation succeeded")
        return {"statusCode": 200, "status": "success", "details": message}
    else:
        message = (
            f"⚠️ **[Deploy Failed]** 배포 검증 실패!\n"
            f"**환경:** `{env_display}`\n"
            f"**Pipeline ID:** `{pipeline_id}`\n"
            f"**사유:** {reason}\n"
            f"**확인 URL:** {CHECK_URL}\n"
            f"**로그:** {log_url}"
        )
        send_discord_notification(message)
        print("❌ Validation failed")
        return {"statusCode": 500, "status": "failed", "details": message}


def get_auto_check_url():
    """
    Elastic Beanstalk 환경 설정에서 도메인 + 헬스체크 경로를 자동으로 가져옴
    환경 ID 우선, 환경 이름 대체
    """
    try:
        # 환경 조회 (ID 우선, 이름 대체)
        if BEANSTALK_ENV_ID:
            print(f"🔍 Looking up EB environment by ID: {BEANSTALK_ENV_ID}")
            envs = beanstalk.describe_environments(EnvironmentIds=[BEANSTALK_ENV_ID])
        elif BEANSTALK_ENV_NAME:
            print(f"🔍 Looking up EB environment by name: {BEANSTALK_ENV_NAME}")
            envs = beanstalk.describe_environments(EnvironmentNames=[BEANSTALK_ENV_NAME])
        else:
            print("❌ No environment ID or name provided")
            return None
        
        if not envs.get("Environments"):
            print(f"❌ No environment found")
            return None
        
        env = envs["Environments"][0]
        env_name = env.get("EnvironmentName")
        cname = env.get("CNAME", "")
        
        print(f"✅ Found environment: {env_name}")
        
        if not cname:
            print(f"❌ No CNAME found for environment")
            return None
        
        if not cname.startswith("http"):
            cname = "http://" + cname
        
        print(f"✅ Found CNAME: {cname}")

        # 헬스체크 경로 가져오기 (환경 이름 필요)
        try:
            print(f"🔍 Getting configuration for: {env_name}")
            settings = beanstalk.describe_configuration_settings(EnvironmentName=env_name)
            option_settings = settings["ConfigurationSettings"][0]["OptionSettings"]
            health_path = "/"
            
            for opt in option_settings:
                if (
                    opt["Namespace"] == "aws:elasticbeanstalk:environment:process:default"
                    and opt["OptionName"] == "HealthCheckPath"
                ):
                    health_path = opt["Value"]
                    print(f"✅ Found HealthCheckPath: {health_path}")
                    break
        except Exception as e:
            print(f"⚠️ Could not get HealthCheckPath, using default '/': {e}")
            health_path = "/"

        final_url = cname.rstrip("/") + health_path
        print(f"✅ Constructed CHECK_URL: {final_url}")
        return final_url
        
    except Exception as e:
        print(f"⚠️ Failed to auto-detect CHECK_URL: {str(e)}")
        import traceback
        print(f"Stack trace: {traceback.format_exc()}")
        return None


def describe_environment_health():
    """
    환경 헬스 상태 조회 (ID 우선, 이름 대체)
    """
    try:
        if BEANSTALK_ENV_ID:
            # 환경 ID로 먼저 환경 이름 가져오기
            envs = beanstalk.describe_environments(EnvironmentIds=[BEANSTALK_ENV_ID])
            if envs.get("Environments"):
                env_name = envs["Environments"][0].get("EnvironmentName")
                return beanstalk.describe_environment_health(
                    EnvironmentName=env_name,
                    AttributeNames=['Color', 'HealthStatus']
                )
        elif BEANSTALK_ENV_NAME:
            return beanstalk.describe_environment_health(
                EnvironmentName=BEANSTALK_ENV_NAME,
                AttributeNames=['Color', 'HealthStatus']
            )
        return None
    except Exception as e:
        print(f"⚠️ Failed to get environment health: {e}")
        return None


def parse_pipeline_event(event):
    """DynamoDB 형식 또는 일반 형식의 이벤트를 파싱"""
    try:
        # DynamoDB 직접 형식인 경우 (테스트 이벤트)
        if 'pipelineID' in event and 'S' in event.get('pipelineID', {}):
            return parse_dynamodb_item(event)
        
        # 일반 JSON 형식인 경우
        if 'pipelineID' in event and isinstance(event['pipelineID'], str):
            return event
        
        return None
    except Exception as e:
        print(f"Error parsing event: {e}")
        return None


def parse_dynamodb_item(item):
    """DynamoDB 항목 형식을 일반 Python dict로 변환"""
    result = {}
    
    if 'pipelineID' in item and 'S' in item['pipelineID']:
        result['pipelineID'] = item['pipelineID']['S']
    
    if 'currentStage' in item and 'S' in item['currentStage']:
        result['currentStage'] = item['currentStage']['S']
    
    if 'status' in item and 'S' in item['status']:
        result['status'] = item['status']['S']
    
    if 'errorMessage' in item and 'S' in item.get('errorMessage', {}):
        result['errorMessage'] = item['errorMessage']['S']
    
    if 'logUrl' in item and 'S' in item.get('logUrl', {}):
        result['logUrl'] = item['logUrl']['S']
    
    if 'totalStages' in item and 'N' in item.get('totalStages', {}):
        result['totalStages'] = int(item['totalStages']['N'])
    
    if 'stageList' in item and 'L' in item.get('stageList', {}):
        result['stageList'] = [stage['S'] for stage in item['stageList']['L']]
    
    if 'aiSolution' in item and 'S' in item.get('aiSolution', {}):
        result['aiSolution'] = item['aiSolution']['S']
    
    return result


def send_discord_notification(message):
    if not DISCORD_URL:
        print("No Discord webhook URL set.")
        return
    try:
        url = http.client.urlsplit(DISCORD_URL)
        conn = http.client.HTTPSConnection(url.hostname)
        payload = json.dumps({'content': message})
        headers = {'Content-Type': 'application/json'}
        conn.request("POST", url.path, payload, headers)
        res = conn.getresponse()
        print(f"Discord response: {res.status}")
        conn.close()
    except Exception as e:
        print(f"Error sending Discord notification: {e}")
