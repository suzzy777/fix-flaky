run for fastjson97ee7b6test_for_issue5 (ID) using this below command in the extracted folder::


python3 flakyguard.py --repo fastjson --test-file src/test/java/com/alibaba/json/bvt/issue_1400/Issue1480.java --test-func test_for_issue --test-case test_for_issue --language java --repro-script single_runner.sh --repro-config-csv test_config.csv --repro-issue-id fastjson97ee7b6test_for_issue5 --use-jacoco-coverage  --context-attempts 3 --thoughts-per-context 2 --fixes-per-thought 3

run for dubbo OD using: 

python3 flakyguard.py --repo dubbo/dubbo-rpc/dubbo-rpc-dubbo --test-file src/test/java/org/apache/dubbo/rpc/protocol/dubbo/telnet/ChangeTelnetHandlerTest.java --test-func testChangeServiceNotExport --test-case testChangeServiceNotExport --language java --repro-script single_runner.sh --repro-config-csv test_config.csv --repro-issue-id dubbodubborpcdubborpcdubboaa9f16e --use-jacoco-coverage --con\
text-attempts 3 --thoughts-per-context 2 --fixes-per-thought 3 > od_run_dubbodubborpcdubborpcdubboaa9f16e.log

If you want to try on a new project from the test_config.csv, look in github to clone the projects for now. The SHA can be found in the last part of the container name in the command (dubbodubborpcdubborpcdubboaa9f16e). 
You need to clone the new project in the same directory, checkout to the required SHA for the graph part and give that location above in --repo argument. 