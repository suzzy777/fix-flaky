#!/bin/bash

TEST_FOLDER_NAME=$1
DATA_FOLDER=$2
MODULE=$3
FULL_TEST_NAME=$4
ITERATIONS=${5:-5}
CODE_VERSION=${6:-"All"}  

BASE_IMAGE_NAME="flaky_base_jdk8_cover"
CONTAINER_NAME="Nio$TEST_FOLDER_NAME"
DIR_TO_PYTHON_SCRIPT="/app/source"
BASE_DIR="data/${TEST_FOLDER_NAME}"
ZIP_DATA_CONTAINER="data/${DATA_FOLDER}"

if [ -f "${ZIP_DATA_CONTAINER}.zip" ]; then
    mkdir -p "${BASE_DIR}"
    unzip -o "${ZIP_DATA_CONTAINER}.zip" -d "${BASE_DIR}" > /dev/null || { echo "Failed to unzip ${ZIP_DATA_CONTAINER}.zip"; exit 1; }
    if [ -d "${BASE_DIR}/${DATA_FOLDER}" ]; then
        mv "${BASE_DIR}/${DATA_FOLDER}/"* "${BASE_DIR}/"
        rmdir "${BASE_DIR}/${DATA_FOLDER}"
    fi
fi

FLAKY_DIR="${BASE_DIR}/Flaky"
FLAKY_M2_DIR="${BASE_DIR}/Flakym2/.m2"
FIXED_DIR="${BASE_DIR}/Fixed"
FIXED_PATCH="${BASE_DIR}/Fixed.patch"
RESULT_DIR="${BASE_DIR}/result"


if [ -d "${BASE_DIR}/Fixedm2" ]; then
    FIXED_M2_DIR="${BASE_DIR}/Fixedm2/.m2"
else
    FIXED_M2_DIR="${BASE_DIR}/Flakym2/.m2"
fi

if [ -d "$FLAKY_DIR" ]; then
    cp jacocoagent.jar "$FLAKY_DIR/" || { echo "Failed to copy jacocoagent.jar"; exit 1; }
    cp jacococli.jar "$FLAKY_DIR/" || { echo "Failed to copy jacococli.jar"; exit 1; }
    cp coverage_generator.sh "$FLAKY_DIR/" || { echo "Failed to copy coverage_generator.sh"; exit 1; }
    cp -r python-scripts "$FLAKY_DIR/" || { echo "Failed to copy Python scripts"; exit 1; }
    cp -r testrunner "$FLAKY_DIR/" || { echo "Failed to copy testrunner"; exit 1; }
    cp -r iDFlakies "$FLAKY_DIR/" || { echo "Failed to copy iDFlakies"; exit 1; }
    cp nio_statistics_generator.sh "$FLAKY_DIR/" || { echo "Failed to copy nio_statistics_generator.sh"; exit 1; }
    cp modify_pom_for_coverage.sh "$FLAKY_DIR/" || { echo "Failed to copy modify_pom_for_coverage.sh"; exit 1; }
else
    sleep 5
fi

if [ -d "$RESULT_DIR" ]; then
    rm -rf "$RESULT_DIR"
fi

create_folder_with_patch() {
    BASE_DIR=$1
    PATCH_FILE=$2
    TARGET_DIR=$3
    echo "Creating folder: $TARGET_DIR using patch: $PATCH_FILE..."
    rm -rf "$TARGET_DIR"  
    cp -r "$BASE_DIR" "$TARGET_DIR" || { echo "Failed to copy $BASE_DIR to $TARGET_DIR";  }
    patch -p1 -d "$TARGET_DIR" < "$PATCH_FILE" || { echo "Failed to apply patch $PATCH_FILE to $TARGET_DIR"; }
    echo "Successfully created $TARGET_DIR."
}


if [[ "$CODE_VERSION" == "All" || "$CODE_VERSION" == "Fixed" ]]; then
    if [[ ! -d "$FIXED_DIR" ]]; then
        create_folder_with_patch "$FLAKY_DIR" "$FIXED_PATCH" "$FIXED_DIR"
    fi
fi

SOURCE_DIRS=()
M2_DIRS=()

case "$CODE_VERSION" in
    "All")
        SOURCE_DIRS=("$FLAKY_DIR" "$FIXED_DIR" )
        M2_DIRS=("$FLAKY_M2_DIR" "$FIXED_M2_DIR" )
        ;;
    "Flaky")
        SOURCE_DIRS=("$FLAKY_DIR")
        M2_DIRS=("$FLAKY_M2_DIR")
        ;;
    "Fixed")
        SOURCE_DIRS=("$FIXED_DIR")
        M2_DIRS=("$FIXED_M2_DIR")
        ;;
  
    *)
        sleep 5

        ;;
esac

mkdir -p "$RESULT_DIR"

docker build -t $BASE_IMAGE_NAME -f Dockerfile.nio .

for i in "${!SOURCE_DIRS[@]}"; do

    SRC_DIR="${SOURCE_DIRS[$i]}"
    M2_DIR="${M2_DIRS[$i]}"
    DIR_NAME=$(basename "$SRC_DIR")
    FLAKY_RESULT_DIR="$RESULT_DIR/$DIR_NAME"
    HOST_SRC_ABS="$(readlink -f "$SRC_DIR")"
    HOST_M2_ABS="$(readlink -f "$M2_DIR")"
    [[ -d "$HOST_SRC_ABS" ]] || { echo "Missing source dir: $HOST_SRC_ABS"; exit 1; }
    [[ -d "$HOST_M2_ABS" ]] || { echo "Missing m2 dir: $HOST_M2_ABS"; exit 1; }
    
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER_NAME" \
    --mount type=bind,source="$HOST_SRC_ABS",target=/app/source \
    --mount type=bind,source="$HOST_M2_ABS",target=/root/.m2 \
    "$BASE_IMAGE_NAME" \
    tail -f /dev/null
     docker exec -i $CONTAINER_NAME /bin/bash -c "cd /app/source && chmod +x nio_statistics_generator.sh && ./nio_statistics_generator.sh \"$MODULE\" \"$DIR_TO_PYTHON_SCRIPT\" \"$FULL_TEST_NAME\" \"$ITERATIONS\""
    #docker exec -i $CONTAINER_NAME /bin/bash -c "cd /app/source && chmod +x nio_statistics_generator.sh && ./nio_statistics_generator.sh \"$MODULE\" \"$FULL_TEST_NAME\" \"$ITERATIONS\""

    echo "Checking failing test output inside container..."
    docker exec -i "$CONTAINER_NAME" /bin/bash -c \
	   'ls -lah /app/source/failing-test-output-* 2>/dev/null || true'

    docker exec -i "$CONTAINER_NAME" /bin/bash -c \
	   'cat /app/source/failing-test-output-*/* 2>/dev/null || true'
    
    mkdir -p "$FLAKY_RESULT_DIR"

    # mkdir -p "$FLAKY_RESULT_DIR/failing-test-output"

    # if [[ -d "$SRC_DIR/flaky-result" ]]; then
    # 	cp -av "$SRC_DIR/flaky-result/." "$FLAKY_RESULT_DIR/"
    # else
    # 	echo "ERROR: $SRC_DIR/flaky-result does not exist"
    # fi

    # cp -a "$SRC_DIR"/failing-test-output-* "$FLAKY_RESULT_DIR/failing-test-output/" 2>/dev/null || true

    # cp -a "$SRC_DIR/flaky-result/." "$FLAKY_RESULT_DIR/"
    mkdir -p "$FLAKY_RESULT_DIR/testlog"
    mkdir -p "$FLAKY_RESULT_DIR/failing-test-output"

    echo "DEBUG DIR_NAME=$DIR_NAME"
    echo "DEBUG SRC_DIR=$SRC_DIR"
    echo "DEBUG FLAKY_RESULT_DIR=$FLAKY_RESULT_DIR"

    echo "DEBUG files in SRC_DIR:"
    find "$SRC_DIR" -maxdepth 3 -type f | head -50

    echo "DEBUG flaky-result:"
    find "$SRC_DIR/flaky-result" -maxdepth 4 -type f 2>/dev/null || echo "NO flaky-result"

    # Copy direct mvn logs if they are in source root
    cp -av "$SRC_DIR"/mvn-test-*.log "$FLAKY_RESULT_DIR/testlog/" 2>/dev/null || true

    # Copy failing-test-output dirs if they exist
    cp -av "$SRC_DIR"/failing-test-output-* "$FLAKY_RESULT_DIR/failing-test-output/" 2>/dev/null || true

    # Copy normal flaky-result contents if created
    if [[ -d "$SRC_DIR/flaky-result" ]]; then
	cp -av "$SRC_DIR/flaky-result/." "$FLAKY_RESULT_DIR/"
    else
	echo "ERROR: $SRC_DIR/flaky-result does not exist"
    fi

   
    # mkdir -p "$FLAKY_RESULT_DIR/surefire-reports"
    # find "$SRC_DIR" -path "*/target/surefire-reports/TEST-*.xml" -type f -exec cp -v {} "$FLAKY_RESULT_DIR/surefire-reports/" \;

    docker stop $CONTAINER_NAME
    docker rm $CONTAINER_NAME
     chown -R "$(id -u):$(id -g)" "$SRC_DIR" 2>/dev/null || true
     rm -rf "$SRC_DIR" 2>/dev/null || docker run --rm -v "$(dirname "$HOST_SRC_ABS")":/host "$BASE_IMAGE_NAME" /bin/bash -lc "rm -rf \"/host/$(basename "$HOST_SRC_ABS")\""

done
for _m2 in "${M2_DIRS[@]}"; do
  _m2_abs="$(readlink -f "$_m2")"
  rm -rf "$_m2_abs" 2>/dev/null || docker run --rm -v "$(dirname "$_m2_abs")":/host "$BASE_IMAGE_NAME" /bin/bash -lc "rm -rf \"/host/$(basename "$_m2_abs")\""
done
