package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

type recorded struct {
	method, path, query, auth string
	body                      map[string]any
}

func fakeAPI(t *testing.T) (*httptest.Server, *[]recorded) {
	t.Helper()
	var mu sync.Mutex
	var calls []recorded
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := recorded{method: r.Method, path: r.URL.Path, query: r.URL.RawQuery, auth: r.Header.Get("Authorization")}
		if r.Body != nil {
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &rec.body)
		}
		mu.Lock()
		calls = append(calls, rec)
		mu.Unlock()
		switch r.URL.Path {
		case "/v1/analytics/sites":
			_ = json.NewEncoder(w).Encode(map[string]any{"sites": []any{map[string]any{"id": "site_1", "domain": "example.com"}}})
		case "/v1/analytics/funnel":
			_ = json.NewEncoder(w).Encode(map[string]any{"steps": []any{
				map[string]any{"event": "page_view:/signup", "sessions": 10},
				map[string]any{"event": "sign_up_completed|sign_in_completed", "sessions": 4},
			}})
		case "/v1/query":
			_ = json.NewEncoder(w).Encode(map[string]any{"interpretation": "sign-ups by page", "figure": map[string]any{"data": []any{}}, "chart_id": "c1"})
		case "/v1/collect":
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotFound)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "not_found"})
		}
	}))
	t.Cleanup(server.Close)
	return server, &calls
}

func runMCP(t *testing.T, apiURL string, messages ...string) []map[string]any {
	t.Helper()
	var out, errOut bytes.Buffer
	in := strings.NewReader(strings.Join(messages, "\n") + "\n")
	if err := run([]string{"--api-url", apiURL, "--api-key", "th_test", "mcp"}, in, &out, &errOut); err != nil {
		t.Fatalf("mcp: %v (%s)", err, errOut.String())
	}
	var responses []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		if line == "" {
			continue
		}
		var msg map[string]any
		if err := json.Unmarshal([]byte(line), &msg); err != nil {
			t.Fatalf("bad response line %q: %v", line, err)
		}
		responses = append(responses, msg)
	}
	return responses
}

func toolResultText(t *testing.T, response map[string]any) (string, bool) {
	t.Helper()
	result, ok := response["result"].(map[string]any)
	if !ok {
		t.Fatalf("no result: %#v", response)
	}
	content := result["content"].([]any)
	return content[0].(map[string]any)["text"].(string), result["isError"].(bool)
}

func TestMCPHandshakeAndToolsList(t *testing.T) {
	server, _ := fakeAPI(t)
	responses := runMCP(t, server.URL,
		`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}`,
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`,
		`{"jsonrpc":"2.0","id":3,"method":"bogus/method"}`,
		`not json`,
		`{"jsonrpc":"2.0","id":"p","method":"ping"}`,
	)
	if len(responses) != 5 {
		t.Fatalf("want 5 responses (notification gets none), got %d: %#v", len(responses), responses)
	}
	init := responses[0]["result"].(map[string]any)
	if init["protocolVersion"] != "2025-06-18" {
		t.Fatalf("protocolVersion = %v", init["protocolVersion"])
	}
	if _, ok := init["capabilities"].(map[string]any)["tools"]; !ok {
		t.Fatal("tools capability missing")
	}
	if init["serverInfo"].(map[string]any)["name"] != "twohelixes" {
		t.Fatalf("serverInfo = %v", init["serverInfo"])
	}
	tools := responses[1]["result"].(map[string]any)["tools"].([]any)
	names := map[string]bool{}
	for _, tool := range tools {
		entry := tool.(map[string]any)
		names[entry["name"].(string)] = true
		if entry["inputSchema"].(map[string]any)["type"] != "object" {
			t.Fatalf("%v inputSchema not an object", entry["name"])
		}
	}
	for _, want := range []string{"list_sites", "summary", "timeseries", "funnel", "paths", "events", "schema", "revenue", "ask", "track"} {
		if !names[want] {
			t.Fatalf("tool %s missing", want)
		}
	}
	if code := responses[2]["error"].(map[string]any)["code"].(float64); code != -32601 {
		t.Fatalf("unknown method code = %v", code)
	}
	if code := responses[3]["error"].(map[string]any)["code"].(float64); code != -32700 {
		t.Fatalf("parse error code = %v", code)
	}
	if responses[4]["id"] != "p" {
		t.Fatalf("ping id = %v", responses[4]["id"])
	}
}

func TestMCPUnknownProtocolFallsBackToLatest(t *testing.T) {
	server, _ := fakeAPI(t)
	responses := runMCP(t, server.URL, `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1999-01-01"}}`)
	if got := responses[0]["result"].(map[string]any)["protocolVersion"]; got != mcpLatestProtocol {
		t.Fatalf("protocolVersion = %v", got)
	}
}

func TestMCPToolCalls(t *testing.T) {
	server, calls := fakeAPI(t)
	responses := runMCP(t, server.URL,
		`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_sites","arguments":{}}}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"funnel","arguments":{"site_id":"site_1","days":7,"steps":["page_view:/signup","sign_up_completed|sign_in_completed"]}}}`,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"ask","arguments":{"site_id":"site_1","question":"where do sign-ups come from?"}}}`,
		`{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"track","arguments":{"write_key":"thw_test","event":"paywall_open","user_id":"u1","props":{"surface":"editor"}}}}`,
		`{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"summary","arguments":{}}}`,
		`{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"revenue","arguments":{"site_id":"missing"}}}`,
		`{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"nope","arguments":{}}}`,
	)
	if len(responses) != 7 {
		t.Fatalf("got %d responses", len(responses))
	}
	text, isErr := toolResultText(t, responses[0])
	if isErr || !strings.Contains(text, "example.com") {
		t.Fatalf("list_sites = %q (err=%v)", text, isErr)
	}
	text, isErr = toolResultText(t, responses[1])
	if isErr || !strings.Contains(text, "sign_up_completed|sign_in_completed") {
		t.Fatalf("funnel = %q", text)
	}
	text, isErr = toolResultText(t, responses[2])
	if isErr || !strings.Contains(text, "sign-ups by page") || strings.Contains(text, "figure") {
		t.Fatalf("ask = %q", text)
	}
	if _, isErr = toolResultText(t, responses[3]); isErr {
		t.Fatal("track failed")
	}
	if text, isErr = toolResultText(t, responses[4]); !isErr || !strings.Contains(text, "site_id") {
		t.Fatalf("summary without site = %q", text)
	}
	if text, isErr = toolResultText(t, responses[5]); !isErr || !strings.Contains(text, "404") {
		t.Fatalf("revenue 404 = %q", text)
	}
	if responses[6]["error"].(map[string]any)["code"].(float64) != -32602 {
		t.Fatalf("unknown tool = %#v", responses[6])
	}

	byPath := map[string]recorded{}
	for _, call := range *calls {
		byPath[call.path] = call
	}
	funnel := byPath["/v1/analytics/funnel"]
	if !strings.Contains(funnel.query, "steps=page_view%3A%2Fsignup%2Csign_up_completed%7Csign_in_completed") || !strings.Contains(funnel.query, "days=7") {
		t.Fatalf("funnel query = %s", funnel.query)
	}
	if funnel.auth != "Bearer th_test" {
		t.Fatalf("auth = %q", funnel.auth)
	}
	query := byPath["/v1/query"].body
	if query["analytics_site_id"] != "site_1" || query["q"] != "where do sign-ups come from?" {
		t.Fatalf("query body = %#v", query)
	}
	collect := byPath["/v1/collect"].body
	if collect["site_id"] != "thw_test" || collect["event"] != "paywall_open" || collect["user_id"] != "u1" {
		t.Fatalf("collect body = %#v", collect)
	}
	if collect["props"].(map[string]any)["surface"] != "editor" {
		t.Fatalf("collect props = %#v", collect["props"])
	}
}
