import os
import subprocess
import re
import tempfile
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

SCRIPT_FILE_PATTERNS = ['*.py', '*.lua', '*.js', '*.ts', '*.java', '*.rb', '*.php', '*.html', '*.css']
GITHUB_TOKEN = 'token'
VERBOSE = 0
USE_SPARSE_CHECKOUT = 0
limit = 0
max_repos = 0
file_counter_lock = Lock()
file_counter = 0

def print_verbose(message):
    if VERBOSE:
        print(message)

def run_subprocess(command, cwd=None):
    try:
        if VERBOSE:
            subprocess.run(command, cwd=cwd, check=True)
        else:
            subprocess.run(command, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        print_verbose(f"Subprocess error: {e}")

def fetch_repos(query, max_repos):
    per_page = 100
    repos = []
    page = 1
    while len(repos) < max_repos:
        url = f'https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page={per_page}&page={page}'
        print_verbose(f"Fetching page {page} from {url}")
        response = requests.get(url, headers={'Authorization': f'token {GITHUB_TOKEN}'})
        if response.status_code == 200:
            data = response.json()
            new_repos = data.get('items', [])
            print_verbose(f"Page {page} fetched, {len(new_repos)} repositories found.")
            if not new_repos:
                break  
            repos.extend(new_repos)
            page += 1
            if len(repos) > max_repos:
                repos = repos[:max_repos]
        elif response.status_code == 403: # Rate limit code
            print_verbose(f"Error fetching page {page}: {response.status_code} - {response.text}")
            print_verbose("Rate limit exceeded. Waiting for 30 seconds before retrying...")
            time.sleep(30)  
            continue  
        else:
            print_verbose(f"Error fetching page {page}: {response.status_code} - {response.text}")
            break
    print_verbose(f"Total fetched repositories: {len(repos)} for query '{query}'.")
    return repos

def shallow_clone_repo(repo_url, clone_path):
    print_verbose(f"Shallow cloning repository: {repo_url}")
    run_subprocess(['git', 'clone', '--depth', '1', repo_url, clone_path])
    print_verbose(f"Repository shallow cloned to {clone_path}.")

def configure_sparse_checkout(repo_path, patterns): # BROKEN
    sparse_checkout_file = os.path.join(repo_path, '.git', 'info', 'sparse-checkout')
    with open(sparse_checkout_file, 'w') as f:
        for pattern in patterns:
            f.write(pattern + '\n')
    run_subprocess(['git', 'config', 'core.sparseCheckout', 'true'], cwd=repo_path)
    run_subprocess(['git', 'checkout', 'HEAD'], cwd=repo_path)

def analyze_code_content(content, limit):
    print_verbose("Analyzing code content.")
    code_lines = content.splitlines()
    results = check_consecutive_whitespace(code_lines, limit)
    return results

def check_consecutive_whitespace(code_lines, limit):
    excessive_whitespace = []
    pattern = r'[ \t]{' + str(limit) + r',}'
    for i, line in enumerate(code_lines, start=1):
        consecutive_whitespace = re.findall(pattern, line)
        if consecutive_whitespace:
            excessive_whitespace.append((i, line.strip()))
    return excessive_whitespace

def create_detected_folder():
    if not os.path.exists('Detected'):
        os.makedirs('Detected')

def log_findings(repo_name, repo_owner, repo_url, file_path, results):
    global file_counter
    create_detected_folder()
    for i, (line_no, code) in enumerate(results, start=1):
        with file_counter_lock:
            detected_file = os.path.join('Detected', f'detected_{file_counter}.txt')
            file_counter += 1
        with open(detected_file, 'w') as f:
            f.write(f"Repository: {repo_name}\n")
            f.write(f"Owner: {repo_owner}\n")
            f.write(f"Repository URL: {repo_url}\n")
            f.write(f"File: {file_path}\n")
            f.write('-' * 40 + '\n')
            f.write(f"Line {line_no}: {code}\n")
            f.write('-' * 40 + '\n')

def process_repo(repo_url, repo_name, repo_owner, limit=5):
    print_verbose(f"Processing repository: {repo_url}")
    with tempfile.TemporaryDirectory() as tmpdirname:
        clone_path = os.path.join(tmpdirname, 'repo')
        try:
            shallow_clone_repo(repo_url, clone_path)
            if USE_SPARSE_CHECKOUT:
                configure_sparse_checkout(clone_path, SCRIPT_FILE_PATTERNS)
            create_detected_folder()
            for root, dirs, files in os.walk(clone_path):
                if '.git' in dirs:
                    dirs.remove('.git')
                print_verbose(f"Scanning directory: {root}")
                for file in files:
                    if any(file.endswith(pattern.strip('*')) for pattern in SCRIPT_FILE_PATTERNS):
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read()
                                results = analyze_code_content(content, limit)
                                if results:
                                    log_findings(repo_name, repo_owner, repo_url, file_path, results)
                                    print_verbose(f"Issues found in {file_path}. Details logged.")
                                else:
                                    print_verbose(f"No issues found in {file_path}.")
                        except OSError as e:
                            print_verbose(f"Error reading file {file_path}: {e}")
        except Exception as e:
            print_verbose(f"Error during processing: {e}")

def get_boolean_input(prompt):
    response = input(prompt).strip().lower()
    return response == 'yes'

def main():
    global VERBOSE, USE_SPARSE_CHECKOUT, limit, max_repos
    query = input("Enter the search query (or type 'all'): ")
    limit = int(input("Enter the number of consecutive spaces or tabs to check for: "))
    max_repos = int(input("Enter the maximum number of repositories to search for: "))
    VERBOSE = get_boolean_input("Enable verbose output? (yes/no): ")
    USE_SPARSE_CHECKOUT = get_boolean_input("Enable sparse checkout? (yes/no): ")
    
    search_query = 'stars:>=0' if query.lower() == 'all' else f'topic:{query}'
    print_verbose(f"Fetching repositories for query: {search_query}")
    repos = fetch_repos(search_query, max_repos)
    num_cores = os.cpu_count()
    max_workers = max(1, num_cores - 1)
    print_verbose(f"Using {max_workers} worker threads.")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                process_repo,
                repo['clone_url'],
                repo['name'],
                repo['owner']['login'],
                limit
            )
            for repo in repos
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print_verbose(f"Thread error: {e}")

if __name__ == "__main__":
    main()
