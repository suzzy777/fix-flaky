#!/usr/bin/env python3
import csv
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

INPUT_CSV = "OD_unfixed_3.csv"
WORKDIR = Path("work")
RESULTS = Path("results")

MVNOPTIONS = [
    "-Ddependency-check.skip=true",
    "-Dgpg.skip=true",
    "-DfailIfNoTests=false",
    "-Dskip.installnodenpm",
    "-Dskip.npm",
    "-Dskip.yarn",
    "-Dlicense.skip",
    "-Dcheckstyle.skip",
    "-Drat.skip",
    "-Denforcer.skip",
    "-Danimal.sniffer.skip",
    "-Dmaven.javadoc.skip",
    "-Dfindbugs.skip",
    "-Dwarbucks.skip",
    "-Dmodernizer.skip",
    "-Dimpsort.skip",
    "-Dmdep.analyze.skip",
    "-Dpgpverify.skip",
    "-Dxml.skip",
    "-Dcobertura.skip=true",
    "-Dspotless.skip=true",
    "-Dspotless.check.skip=true",
    "-Dossindex.skip=true",
    "-Dmaven.bundle.plugin.skip=true",
    "-Dmaven.parallel.force=false",
]


def run(cmd, cwd=None, log=None):
    print(" ".join(cmd))
    if log:
        with open(log, "w") as f:
            return subprocess.run(
                cmd,
                cwd=cwd,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
            ).returncode
    return subprocess.run(cmd, cwd=cwd).returncode


def test_to_surefire(test_name):
    cls, method = test_name.rsplit(".", 1)
    return f"{cls}#{method}"


def parse_surefire_tests(report_dir, out_file):
    tests = set()

    for xml_file in report_dir.glob("TEST-*.xml"):
        try:
            root = ET.parse(xml_file).getroot()
        except ET.ParseError:
            continue

        for tc in root.iter("testcase"):
            cls = tc.attrib.get("classname")
            name = tc.attrib.get("name")
            if cls and name:
                tests.add(f"{cls}.{name}")

    tests = sorted(tests)
    out_file.write_text("\n".join(tests) + ("\n" if tests else ""))
    return tests


def testcase_status(report_dir, full_test_name):
    target_cls, target_method = full_test_name.rsplit(".", 1)

    for xml_file in report_dir.glob("TEST-*.xml"):
        try:
            root = ET.parse(xml_file).getroot()
        except ET.ParseError:
            continue

        for tc in root.iter("testcase"):
            cls = tc.attrib.get("classname")
            name = tc.attrib.get("name")

            if cls == target_cls and name == target_method:
                has_failure = tc.find("failure") is not None
                has_error = tc.find("error") is not None
                has_skipped = tc.find("skipped") is not None

                if has_failure:
                    return "FAILURE"
                if has_error:
                    return "ERROR"
                if has_skipped:
                    return "SKIPPED"
                return "PASS"

    return "NOT_FOUND"


def main():
    WORKDIR.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)

    with open(INPUT_CSV, newline="") as f:
        reader = csv.reader(f)

        for row in reader:
            if not row or row[0].startswith("#"):
                continue

            repo, sha, module, victim = row[:4]

            project = repo.rstrip("/").split("/")[-1].replace(".git", "")
            repo_dir = WORKDIR / f"{project}_{sha[:8]}"
            out_dir = RESULTS / f"{project}_{sha[:8]}_{module.replace('/', '_')}"
            out_dir.mkdir(parents=True, exist_ok=True)

            print(f"\n=== {project} {sha} {module} {victim} ===")

            if not repo_dir.exists():
                run(["git", "clone", repo, str(repo_dir)])

            run(["git", "fetch", "--all"], cwd=repo_dir)
            run(["git", "checkout", sha], cwd=repo_dir)

            module_log = out_dir / "module-test.log"

            run(
                [
                    "mvn",
                    "-pl",
                    module,
                    "-am",
                    "test",
                    *MVNOPTIONS,
                ],
                cwd=repo_dir,
                log=module_log,
            )

            report_dir = repo_dir / module / "target" / "surefire-reports"
            test_list_file = out_dir / "test-list.txt"

            tests = parse_surefire_tests(report_dir, test_list_file)
            print(f"Found {len(tests)} tests")

            victim_spec = test_to_surefire(victim)
            results_csv = out_dir / "pair-results.csv"

            with open(results_csv, "w", newline="") as rf:
                writer = csv.writer(rf)
                writer.writerow(
                    [
                        "candidate_test",
                        "victim_test",
                        "candidate_status",
                        "victim_status",
                        "maven_exit_code",
                        "log",
                    ]
                )

                for candidate in tests:
                    if candidate == victim:
                        continue

                    candidate_spec = test_to_surefire(candidate)
                    safe_name = candidate.replace("/", "_").replace(".", "_").replace("#", "_")
                    log_file = out_dir / f"pair_{safe_name}.log"

                    # Clean old reports so status is only from this pair run.
                    surefire_dir = repo_dir / module / "target" / "surefire-reports"
                    if surefire_dir.exists():
                        for p in surefire_dir.glob("*"):
                            if p.is_file():
                                p.unlink()

                    rc = run(
                        [
                            "mvn",
                            "test",
                            "-Dsurefire.runOrder=testorder",
                            f"-Dtest={candidate_spec},{victim_spec}",
                            "-pl",
                            module,
                            *MVNOPTIONS,
                        ],
                        cwd=repo_dir,
                        log=log_file,
                    )

                    candidate_status = testcase_status(surefire_dir, candidate)
                    victim_status = testcase_status(surefire_dir, victim)

                    writer.writerow(
                        [
                            candidate,
                            victim,
                            candidate_status,
                            victim_status,
                            rc,
                            str(log_file),
                        ]
                    )

            print(f"Saved: {test_list_file}")
            print(f"Saved: {results_csv}")


if __name__ == "__main__":
    main()


    
