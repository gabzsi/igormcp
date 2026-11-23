// Portable report controller: Windows + Igor only, statically linked runtime.
// A loopback-only, token-scoped HTTP connection bridges HTML and a single editor.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shellapi.h>
#include <shlwapi.h>
#include <objbase.h>
#include <tlhelp32.h>
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
static fs::path root;
static std::vector<std::string> figures;
static HANDLE editor = nullptr;
static DWORD editorPid = 0;
static int editorIndex = -1;
static bool editorShown = false;
static unsigned long long revision = 0;
static std::vector<fs::file_time_type> previewTimes;
static bool running = true;
static ULONGLONG lastRequest = GetTickCount64();
static HWND editorWindow();

static std::string readFile(const fs::path& path) {
    std::ifstream stream(path, std::ios::binary);
    if (!stream) throw std::runtime_error("A report file is missing. Keep the complete report folder together.");
    return std::string(std::istreambuf_iterator<char>(stream), {});
}
static std::string json(const std::string& value) {
    std::string out = "\"";
    for (unsigned char c : value) {
        if (c == '\\' || c == '"') { out += '\\'; out += char(c); }
        else if (c >= 32) out += char(c);
    }
    return out + "\"";
}
static std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {return char(std::tolower(c));});
    return value;
}
static fs::path figurePath(int index, const wchar_t* extension) {
    return root / L"figures" / (fs::path(figures.at(index)).wstring() + extension);
}
static void pollEditor() {
    if (editor && WaitForSingleObject(editor, 0) == WAIT_OBJECT_0) {
        CloseHandle(editor); editor = nullptr; editorPid = 0; editorIndex = -1; ++revision;
    }
    if (editor && !editorShown) {
        HWND hwnd = editorWindow();
        if (hwnd) {
            ShowWindow(hwnd, SW_SHOWNORMAL);
            SetForegroundWindow(hwnd);
            editorShown = true;
        }
    }
    for (size_t i = 0; i < figures.size(); ++i) {
        std::error_code error;
        auto current = fs::last_write_time(figurePath(int(i), L".png"), error);
        if (!error && current != previewTimes[i]) { previewTimes[i] = current; ++revision; }
    }
}
struct WindowSearch { DWORD pid; HWND window; std::wstring title; };
static BOOL CALLBACK findWindow(HWND hwnd, LPARAM parameter) {
    auto& search = *reinterpret_cast<WindowSearch*>(parameter);
    DWORD pid = 0; GetWindowThreadProcessId(hwnd, &pid);
    if (pid != search.pid || GetWindow(hwnd, GW_OWNER)) return TRUE;
    wchar_t title[1024] = {};
    GetWindowTextW(hwnd, title, 1024);
    if (std::wstring(title).find(search.title) != std::wstring::npos) search.window = hwnd;
    return TRUE;
}
static HWND editorWindow() {
    if (!editor || editorIndex < 0) return nullptr;
    WindowSearch search{editorPid, nullptr, fs::path(figures[editorIndex]).wstring()};
    EnumWindows(findWindow, reinterpret_cast<LPARAM>(&search));
    return search.window;
}
static fs::path igorExecutable() {
    wchar_t executable[32768] = {};
    DWORD length = 32768;
    HRESULT result = AssocQueryStringW(ASSOCF_NONE, ASSOCSTR_EXECUTABLE, L".pxp", L"open", executable, &length);
    if (FAILED(result) || !fs::is_regular_file(executable))
        throw std::runtime_error("Igor is not associated with .pxp files on this computer. Open a .pxp with Igor once, then reopen this report.");
    std::wstring name = fs::path(executable).filename().wstring();
    std::transform(name.begin(), name.end(), name.begin(), ::towlower);
    if (name != L"igor64.exe" && name != L"igor.exe")
        throw std::runtime_error("The .pxp file association does not point to Igor Pro.");
    return executable;
}
static void openFigure(int index) {
    pollEditor();
    if (editor) {
        if (index != editorIndex) throw std::runtime_error("Close the current Igor figure before opening another.");
        HWND hwnd = editorWindow();
        if (hwnd) { ShowWindow(hwnd, SW_RESTORE); SetForegroundWindow(hwnd); }
        return;
    }
    fs::path exe = igorExecutable();
    std::wstring command = L"\"" + exe.wstring() + L"\" /I \"" + figurePath(index, L".pxp").wstring() +
        L"\" \"" + (root / L"figures" / L"ReportPreview.ipf").wstring() + L"\"";
    std::vector<wchar_t> argument(command.begin(), command.end()); argument.push_back(0);
    STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESHOWWINDOW; startup.wShowWindow = SW_SHOWNORMAL;
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(exe.c_str(), argument.data(), nullptr, nullptr, FALSE,
        CREATE_NEW_PROCESS_GROUP, nullptr, root.c_str(), &startup, &process))
        throw std::runtime_error("Windows could not open this figure in Igor.");
    CloseHandle(process.hThread);
    editor = process.hProcess; editorPid = process.dwProcessId; editorIndex = index; editorShown = false; ++revision;
    // No automation reference and no job object ties Igor's lifetime to this program.
}
static void closeEditorSafely() {
    HWND hwnd = editorWindow();
    if (editor && !hwnd) throw std::runtime_error("Igor is still opening or has changed documents. Close it normally in Igor.");
    if (!hwnd) return;
    // CLI dispatch is safe only when the sole Igor process is this editor.
    // Otherwise retain Igor's normal close/save prompt for its own window.
    int igorCount = 0;
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot != INVALID_HANDLE_VALUE) {
        PROCESSENTRY32W process{}; process.dwSize = sizeof(process);
        if (Process32FirstW(snapshot, &process)) do {
            if (_wcsicmp(process.szExeFile, L"Igor64.exe") == 0 || _wcsicmp(process.szExeFile, L"Igor.exe") == 0) ++igorCount;
        } while (Process32NextW(snapshot, &process));
        CloseHandle(snapshot);
    }
    if (igorCount != 1) { PostMessageW(hwnd, WM_CLOSE, 0, 0); return; }
    fs::path exe = igorExecutable();
    std::wstring command = L"\"" + exe.wstring() + L"\" /X \"SaveExperiment; Quit\"";
    std::vector<wchar_t> argument(command.begin(), command.end()); argument.push_back(0);
    STARTUPINFOW startup{}; startup.cb = sizeof(startup); startup.dwFlags = STARTF_USESHOWWINDOW; startup.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(exe.c_str(), argument.data(), nullptr, nullptr, FALSE, 0, nullptr, root.c_str(), &startup, &process))
        throw std::runtime_error("Could not ask Igor to save and close. Close its window normally instead.");
    CloseHandle(process.hThread); CloseHandle(process.hProcess);
}
static void sendAll(SOCKET socket, const std::string& data) {
    size_t offset = 0;
    while (offset < data.size()) {
        int amount = send(socket, data.data() + offset, int(std::min<size_t>(data.size() - offset, 65536)), 0);
        if (amount <= 0) return;
        offset += amount;
    }
}
static void reply(SOCKET socket, int status, const std::string& body, const char* type = "application/json") {
    std::string headers = "HTTP/1.1 " + std::to_string(status) + (status == 200 ? " OK\r\n" : " Error\r\n") +
        "Content-Type: " + type + "\r\nContent-Length: " + std::to_string(body.size()) +
        "\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n" +
        "Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'\r\n\r\n";
    sendAll(socket, headers); sendAll(socket, body);
}
static std::string headerValue(const std::string& headers, const std::string& name) {
    std::istringstream lines(headers); std::string line;
    while (std::getline(lines, line)) {
        auto colon = line.find(':');
        if (colon != std::string::npos && lower(line.substr(0, colon)) == name) {
            std::string value = line.substr(colon + 1);
            while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) value.erase(value.begin());
            while (!value.empty() && (value.back() == '\r' || value.back() == ' ')) value.pop_back();
            return value;
        }
    }
    return "";
}
static void handle(SOCKET client, const std::string& prefix, const std::string& host) {
    DWORD timeout = 2000;
    setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
    setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
    std::string request; char buffer[4096];
    while (request.find("\r\n\r\n") == std::string::npos && request.size() < 32768) {
        int count = recv(client, buffer, sizeof(buffer), 0);
        if (count <= 0) return;
        request.append(buffer, count);
    }
    std::istringstream first(request); std::string method, target, protocol;
    first >> method >> target >> protocol;
    auto headerEnd = request.find("\r\n\r\n");
    if (headerEnd == std::string::npos) { reply(client, 400, "{\"ok\":false}"); return; }
    std::string contentLength = headerValue(request, "content-length");
    size_t bodyLength = 0;
    if (!contentLength.empty()) {
        if (contentLength.size() > 4 || contentLength.find_first_not_of("0123456789") != std::string::npos)
            throw std::runtime_error("Invalid request length.");
        bodyLength = std::stoul(contentLength);
    }
    if (bodyLength > 4096) throw std::runtime_error("Request body is too large.");
    while (request.size() - headerEnd - 4 < bodyLength) {
        int count = recv(client, buffer, sizeof(buffer), 0);
        if (count <= 0) return;
        request.append(buffer, count);
    }
    auto query = target.find('?'); if (query != std::string::npos) target.resize(query);
    if (headerValue(request, "host") != host || target.compare(0, prefix.size(), prefix) != 0) {
        reply(client, 404, "{\"ok\":false,\"error\":\"Unknown report\"}"); return;
    }
    std::string route = target.substr(prefix.size());
    lastRequest = GetTickCount64();
    if (method == "GET" && (route.empty() || route == "report.html")) {
        reply(client, 200, readFile(root / L"report.html"), "text/html; charset=utf-8"); return;
    }
    if (method == "GET" && route == "status") {
        pollEditor(); HWND hwnd = editorWindow();
        reply(client, 200, "{\"ok\":true,\"open\":" + std::string(editor ? "true" : "false") +
            ",\"index\":" + std::to_string(editorIndex) + ",\"pid\":" + std::to_string(editorPid) +
            ",\"visible\":" + std::string(hwnd && IsWindowVisible(hwnd) ? "true" : "false") +
            ",\"revision\":" + std::to_string(revision) + "}"); return;
    }
    if (method == "GET" && route.compare(0, 8, "figures/") == 0) {
        std::string file = route.substr(8);
        for (size_t i = 0; i < figures.size(); ++i) if (file == figures[i] + ".png") {
            reply(client, 200, readFile(figurePath(int(i), L".png")), "image/png"); return;
        }
    }
    if (method != "POST" || headerValue(request, "origin") != "http://" + host ||
        headerValue(request, "content-type") != "application/json") {
        reply(client, 403, "{\"ok\":false,\"error\":\"Use the opened local report\"}"); return;
    }
    if (route.compare(0, 5, "edit/") == 0) {
        std::string indexText = route.substr(5);
        if (indexText.empty() || indexText.size() > 2 || indexText.find_first_not_of("0123456789") != std::string::npos)
            throw std::runtime_error("Unknown figure.");
        int index = std::stoi(indexText);
        if (index < 0 || size_t(index) >= figures.size()) throw std::runtime_error("Unknown figure.");
        openFigure(index); reply(client, 200, "{\"ok\":true}"); return;
    }
    if (route == "close-editor") {
        closeEditorSafely();
        reply(client, 200, "{\"ok\":true}"); return;
    }
    if (route == "shutdown") {
        pollEditor();
        if (editor) throw std::runtime_error("Close the Igor editor first so unsaved work is not discarded.");
        reply(client, 200, "{\"ok\":true}"); running = false; return;
    }
    reply(client, 404, "{\"ok\":false,\"error\":\"Unknown action\"}");
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    bool headless = std::wstring(GetCommandLineW()).find(L"--headless") != std::wstring::npos;
    SOCKET listener = INVALID_SOCKET;
    try {
        wchar_t executable[32768]; GetModuleFileNameW(nullptr, executable, 32768);
        root = fs::path(executable).parent_path();
        std::istringstream manifest(readFile(root / L"figures.txt")); std::string line;
        while (std::getline(manifest, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.empty()) continue;
            if (line.size() > 60 || line.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_") != std::string::npos)
                throw std::runtime_error("Invalid local figure manifest.");
            figures.push_back(line);
        }
        if (figures.empty() || figures.size() > 20) throw std::runtime_error("Expected 1 to 20 local figures.");
        for (size_t i = 0; i < figures.size(); ++i) {
            if (!fs::is_regular_file(figurePath(int(i), L".pxp"))) throw std::runtime_error("A graph PXP is missing.");
            previewTimes.push_back(fs::last_write_time(figurePath(int(i), L".png")));
        }
        igorExecutable();
        WSADATA network{}; if (WSAStartup(MAKEWORD(2, 2), &network) != 0) throw std::runtime_error("Windows networking is unavailable.");
        listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        BOOL exclusive = TRUE; setsockopt(listener, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, reinterpret_cast<char*>(&exclusive), sizeof(exclusive));
        sockaddr_in address{}; address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0 || listen(listener, 8) != 0)
            throw std::runtime_error("Cannot open a local report connection.");
        int size = sizeof(address); getsockname(listener, reinterpret_cast<sockaddr*>(&address), &size);
        GUID guid{}; if (FAILED(CoCreateGuid(&guid))) throw std::runtime_error("Cannot initialize this report session.");
        wchar_t guidText[40]; StringFromGUID2(guid, guidText, 40); std::string token;
        for (wchar_t c : std::wstring(guidText)) if (iswxdigit(c)) token += char(c);
        std::string prefix = "/" + token + "/";
        std::string host = "127.0.0.1:" + std::to_string(ntohs(address.sin_port));
        std::string url = "http://" + host + prefix;
        { std::ofstream session(root / L".session.url", std::ios::trunc); session << url; }
        if (!headless) {
            std::wstring wide(url.begin(), url.end());
            ShellExecuteW(nullptr, L"open", wide.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        }
        while (running) {
            if (!editor && GetTickCount64() - lastRequest > 120000) break;
            pollEditor(); fd_set ready; FD_ZERO(&ready); FD_SET(listener, &ready); timeval wait{0, 250000};
            if (select(0, &ready, nullptr, nullptr, &wait) <= 0) continue;
            SOCKET client = accept(listener, nullptr, nullptr);
            if (client == INVALID_SOCKET) continue;
            try { handle(client, prefix, host); }
            catch (const std::exception& error) { reply(client, 400, "{\"ok\":false,\"error\":" + json(error.what()) + "}"); }
            shutdown(client, SD_SEND);
            closesocket(client);
        }
        closesocket(listener); WSACleanup();
        std::error_code ignored; fs::remove(root / L".session.url", ignored);
        return 0;
    } catch (const std::exception& error) {
        if (listener != INVALID_SOCKET) closesocket(listener);
        if (!root.empty()) { std::ofstream log(root / L".last-error.txt"); log << error.what(); }
        if (!headless) MessageBoxA(nullptr, error.what(), "Igor HTML Report", MB_OK | MB_ICONERROR);
        return 1;
    }
}
