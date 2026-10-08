package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/lee101/twohelixes-cli/internal/client"
)

const mcpLatestProtocol = "2025-11-25"

var mcpProtocols = []string{"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}

const mcpInstructions = `twoHelixes product analytics. Reporting tools take site_id (from list_sites) and days (1-365, default 30).
Start with list_sites, then schema to learn the event names and properties a site actually sends.
Canonical SaaS funnel: page_view, sign_up_started, sign_up_completed, paywall_open, begin_checkout, purchase.
funnel steps are comma-separated and passed through verbatim; servers that support qualifiers accept event:/path (e.g. page_view:/signup) and a|b alternatives.
revenue keeps currencies separate. ask runs the natural-language chart agent over the raw event stream and costs credits.
track sends events with a site write key (thw_...) to /v1/collect.`

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type mcpTool struct {
	Name        string         `json:"name"`
	Title       string         `json:"title,omitempty"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
	Annotations map[string]any `json:"annotations,omitempty"`
	call        func(context.Context, map[string]any) (any, error)
}

type mcpServer struct {
	app      *app
	writeKey string
	tools    []mcpTool
	byName   map[string]*mcpTool
	mu       sync.Mutex
	out      *json.Encoder
}

func (a *app) mcp(args []string) error {
	if len(args) > 0 && (args[0] == "-h" || args[0] == "--help" || args[0] == "help") {
		fmt.Fprintln(a.out, "twohelixes-cli mcp: Model Context Protocol server on stdio (JSON-RPC, newline-delimited).\n\n  claude mcp add twohelixes -e TWOHELIXES_API_KEY=th_... -- twohelixes-cli mcp")
		return nil
	}
	return newMCPServer(a).serve(a.in, a.out)
}

func newMCPServer(a *app) *mcpServer {
	s := &mcpServer{app: a, writeKey: os.Getenv("TWOHELIXES_WRITE_KEY")}
	s.tools = s.defineTools()
	s.byName = make(map[string]*mcpTool, len(s.tools))
	for i := range s.tools {
		s.byName[s.tools[i].Name] = &s.tools[i]
	}
	return s
}

func (s *mcpServer) serve(in io.Reader, out io.Writer) error {
	s.out = json.NewEncoder(out)
	reader := bufio.NewReaderSize(in, 1<<20)
	for {
		line, err := reader.ReadBytes('\n')
		if trimmed := bytes.TrimSpace(line); len(trimmed) > 0 {
			s.handleLine(trimmed)
		}
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

func (s *mcpServer) send(response rpcResponse) {
	response.JSONRPC = "2.0"
	if len(response.ID) == 0 {
		response.ID = json.RawMessage("null")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_ = s.out.Encode(response)
}

func (s *mcpServer) handleLine(line []byte) {
	if line[0] == '[' {
		var batch []json.RawMessage
		if err := json.Unmarshal(line, &batch); err != nil || len(batch) == 0 {
			s.send(rpcResponse{Error: &rpcError{-32700, "parse error"}})
			return
		}
		for _, item := range batch {
			s.handleMessage(item)
		}
		return
	}
	s.handleMessage(line)
}

func (s *mcpServer) handleMessage(raw []byte) {
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		s.send(rpcResponse{Error: &rpcError{-32700, "parse error"}})
		return
	}
	if req.Method == "" {
		return
	}
	notification := len(req.ID) == 0 || string(req.ID) == "null"
	result, rpcErr := s.dispatch(req)
	if notification {
		return
	}
	s.send(rpcResponse{ID: req.ID, Result: result, Error: rpcErr})
}

func (s *mcpServer) dispatch(req rpcRequest) (any, *rpcError) {
	switch req.Method {
	case "initialize":
		var params struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(req.Params, &params)
		negotiated := mcpLatestProtocol
		for _, supported := range mcpProtocols {
			if params.ProtocolVersion == supported {
				negotiated = supported
			}
		}
		return map[string]any{
			"protocolVersion": negotiated,
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo":      map[string]any{"name": "twohelixes", "title": "twoHelixes analytics", "version": version},
			"instructions":    mcpInstructions,
		}, nil
	case "ping":
		return map[string]any{}, nil
	case "tools/list":
		return map[string]any{"tools": s.tools}, nil
	case "tools/call":
		var params struct {
			Name      string         `json:"name"`
			Arguments map[string]any `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &params); err != nil {
			return nil, &rpcError{-32602, "invalid params"}
		}
		tool := s.byName[params.Name]
		if tool == nil {
			return nil, &rpcError{-32602, "unknown tool: " + params.Name}
		}
		if params.Arguments == nil {
			params.Arguments = map[string]any{}
		}
		ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
		defer cancel()
		value, err := tool.call(ctx, params.Arguments)
		if err != nil {
			return toolText(err.Error(), true), nil
		}
		encoded, err := json.MarshalIndent(value, "", " ")
		if err != nil {
			return toolText(err.Error(), true), nil
		}
		return toolText(string(encoded), false), nil
	case "resources/list":
		return map[string]any{"resources": []any{}}, nil
	case "prompts/list":
		return map[string]any{"prompts": []any{}}, nil
	default:
		if strings.HasPrefix(req.Method, "notifications/") {
			return nil, nil
		}
		return nil, &rpcError{-32601, "method not found: " + req.Method}
	}
}

func toolText(text string, isError bool) map[string]any {
	return map[string]any{
		"content": []any{map[string]any{"type": "text", "text": text}},
		"isError": isError,
	}
}

func schema(required []string, props map[string]any) map[string]any {
	out := map[string]any{"type": "object", "properties": props, "additionalProperties": false}
	if len(required) > 0 {
		out["required"] = required
	}
	return out
}

func strProp(description string) map[string]any {
	return map[string]any{"type": "string", "description": description}
}

func intProp(description string, minimum, maximum int) map[string]any {
	return map[string]any{"type": "integer", "description": description, "minimum": minimum, "maximum": maximum}
}

var (
	siteProp = strProp("Analytics site id or domain from list_sites")
	daysProp = intProp("Reporting window in days (default 30)", 1, 365)
	readOnly = map[string]any{"readOnlyHint": true, "openWorldHint": true}
)

func (s *mcpServer) defineTools() []mcpTool {
	return []mcpTool{
		{
			Name: "list_sites", Title: "List analytics sites",
			Description: "List analytics sites the API key can read (id, domain, name, write key).",
			InputSchema: schema(nil, map[string]any{}), Annotations: readOnly,
			call: func(ctx context.Context, _ map[string]any) (any, error) {
				return s.get(ctx, "/v1/analytics/sites", nil)
			},
		},
		{
			Name: "summary", Title: "Traffic summary",
			Description: "Totals (events, users, sessions, page views, bounce rate) plus top pages, events, referrers, devices, browsers, countries and campaigns.",
			InputSchema: schema([]string{"site_id"}, map[string]any{"site_id": siteProp, "days": daysProp}), Annotations: readOnly,
			call: s.report("/v1/analytics/summary"),
		},
		{
			Name: "timeseries", Title: "Traffic over time",
			Description: "Events, page views and users per bucket: hourly up to 8 days, daily beyond.",
			InputSchema: schema([]string{"site_id"}, map[string]any{"site_id": siteProp, "days": daysProp}), Annotations: readOnly,
			call: s.report("/v1/analytics/timeseries"),
		},
		{
			Name: "funnel", Title: "Ordered funnel",
			Description: "Ordered session/user conversion through 2-12 steps with step rate and drop-off. steps is comma-separated and passed through verbatim, e.g. " +
				"`page_view,sign_up_started,sign_up_completed,paywall_open,begin_checkout,purchase`. Where the server supports qualifiers, " +
				"`page_view:/signup` restricts a step to a page path and `sign_up_completed|sign_in_completed` matches either event.",
			InputSchema: schema([]string{"site_id", "steps"}, map[string]any{
				"site_id": siteProp, "days": daysProp,
				"steps": map[string]any{
					"description": "Comma-separated ordered steps, or an array of step strings",
					"anyOf":       []any{map[string]any{"type": "string"}, map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "minItems": 2, "maxItems": 12}},
				},
			}), Annotations: readOnly,
			call: func(ctx context.Context, args map[string]any) (any, error) {
				steps, err := stepsArg(args["steps"])
				if err != nil {
					return nil, err
				}
				q, err := siteQuery(args)
				if err != nil {
					return nil, err
				}
				q.Set("steps", steps)
				return s.get(ctx, "/v1/analytics/funnel", q)
			},
		},
		{
			Name: "paths", Title: "Discovered journeys",
			Description: "Most common session paths without predefined steps. mode: both (default), events, or pages. start filters to paths beginning at a node such as `page_view:/pricing` or `paywall_open`.",
			InputSchema: schema([]string{"site_id"}, map[string]any{
				"site_id": siteProp, "days": daysProp,
				"mode":  map[string]any{"type": "string", "enum": []string{"both", "events", "pages"}},
				"start": strProp("Optional starting event or page node"),
				"depth": intProp("Maximum nodes per path (default 5)", 2, 12),
				"limit": intProp("Maximum paths (default 20)", 1, 100),
			}), Annotations: readOnly,
			call: func(ctx context.Context, args map[string]any) (any, error) {
				q, err := siteQuery(args)
				if err != nil {
					return nil, err
				}
				copyArgs(q, args, "mode", "start", "depth", "limit")
				return s.get(ctx, "/v1/analytics/paths", q)
			},
		},
		{
			Name: "events", Title: "Recent events",
			Description: "The raw recent event stream (newest first) with props, page, device and sampling weight.",
			InputSchema: schema([]string{"site_id"}, map[string]any{
				"site_id": siteProp, "days": daysProp,
				"limit": intProp("Maximum events (default 50)", 1, 1000),
			}), Annotations: readOnly,
			call: func(ctx context.Context, args map[string]any) (any, error) {
				q, err := siteQuery(args)
				if err != nil {
					return nil, err
				}
				if _, ok := args["limit"]; !ok {
					args["limit"] = 50
				}
				copyArgs(q, args, "limit")
				return s.get(ctx, "/v1/analytics/events", q)
			},
		},
		{
			Name: "schema", Title: "Event catalog",
			Description: "Catalog of event names and their custom properties with observed types, source protocols and counts. Call this before building funnels.",
			InputSchema: schema([]string{"site_id"}, map[string]any{
				"site_id": siteProp, "days": daysProp,
				"event": strProp("Optional: describe only this event"),
			}), Annotations: readOnly,
			call: func(ctx context.Context, args map[string]any) (any, error) {
				q, err := siteQuery(args)
				if err != nil {
					return nil, err
				}
				copyArgs(q, args, "event")
				return s.get(ctx, "/v1/analytics/schema", q)
			},
		},
		{
			Name: "revenue", Title: "Revenue",
			Description: "Sampling-weighted net revenue per currency and per event (refunds negative). Reads revenue/value/amount and currency props; currencies are never summed together.",
			InputSchema: schema([]string{"site_id"}, map[string]any{"site_id": siteProp, "days": daysProp}), Annotations: readOnly,
			call: s.report("/v1/analytics/revenue"),
		},
		{
			Name: "ask", Title: "Ask the analytics agent",
			Description: "Natural-language question over the site's raw event stream via POST /v1/query. Returns the interpretation, a data preview, columns and chart_id. Spends account credits.",
			InputSchema: schema([]string{"site_id", "question"}, map[string]any{
				"site_id": siteProp, "days": daysProp,
				"question":       strProp("Question in plain language"),
				"limit":          intProp("Maximum recent event rows to load (default 50000)", 1, 100000),
				"include_figure": map[string]any{"type": "boolean", "description": "Include the Plotly figure JSON (large)"},
			}),
			Annotations: map[string]any{"readOnlyHint": false, "idempotentHint": false, "openWorldHint": true},
			call:        s.ask,
		},
		{
			Name: "track", Title: "Send events",
			Description: "Send one event (event + fields) or a batch (events array) to POST /v1/collect using a site write key. Uses TWOHELIXES_WRITE_KEY when write_key is omitted.",
			InputSchema: schema(nil, map[string]any{
				"write_key":  strProp("Site write key (thw_...) or site id"),
				"event":      strProp("Event name, e.g. sign_up_completed"),
				"client_id":  strProp("Anonymous client id (default twohelixes-mcp)"),
				"user_id":    strProp("Known user id"),
				"session_id": strProp("Session id"),
				"insert_id":  strProp("Idempotency key"),
				"url":        strProp("Page URL"),
				"referrer":   strProp("Referrer URL"),
				"timestamp":  strProp("RFC3339 or Unix seconds/ms/us"),
				"props":      map[string]any{"type": "object", "description": "Event properties, e.g. {\"surface\":\"paywall\",\"revenue\":19,\"currency\":\"USD\"}"},
				"events":     map[string]any{"type": "array", "description": "Native events for a batch; each needs event and client_id", "items": map[string]any{"type": "object"}, "maxItems": 2000},
			}),
			Annotations: map[string]any{"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": true},
			call:        s.track,
		},
	}
}

func (s *mcpServer) requireKey() error {
	if s.app.client.APIKey == "" {
		return errors.New("TWOHELIXES_API_KEY is not set; create one on the twoHelixes account page")
	}
	return nil
}

func (s *mcpServer) get(ctx context.Context, path string, query url.Values) (any, error) {
	if err := s.requireKey(); err != nil {
		return nil, err
	}
	var response any
	err := s.app.client.Do(ctx, http.MethodGet, path+client.Query(query), nil, &response)
	return response, err
}

func (s *mcpServer) report(path string) func(context.Context, map[string]any) (any, error) {
	return func(ctx context.Context, args map[string]any) (any, error) {
		q, err := siteQuery(args)
		if err != nil {
			return nil, err
		}
		return s.get(ctx, path, q)
	}
}

func (s *mcpServer) ask(ctx context.Context, args map[string]any) (any, error) {
	if err := s.requireKey(); err != nil {
		return nil, err
	}
	site, question := argString(args, "site_id"), argString(args, "question")
	if site == "" || question == "" {
		return nil, errors.New("site_id and question are required")
	}
	body := map[string]any{"q": question, "analytics_site_id": site, "days": argInt(args, "days", 30), "limit": argInt(args, "limit", 50000)}
	var response map[string]any
	if err := s.app.client.Do(ctx, http.MethodPost, "/v1/query", body, &response); err != nil {
		return nil, err
	}
	if include, _ := args["include_figure"].(bool); !include {
		for _, key := range []string{"figure", "trace", "audit", "config"} {
			delete(response, key)
		}
	}
	return response, nil
}

func (s *mcpServer) track(ctx context.Context, args map[string]any) (any, error) {
	key := argString(args, "write_key")
	if key == "" {
		key = s.writeKey
	}
	if key == "" {
		return nil, errors.New("write_key is required (or set TWOHELIXES_WRITE_KEY)")
	}
	if raw, ok := args["events"].([]any); ok && len(raw) > 0 {
		if err := s.app.client.Do(ctx, http.MethodPost, "/v1/collect", map[string]any{"site_id": key, "events": raw}, nil); err != nil {
			return nil, err
		}
		return map[string]any{"accepted": len(raw)}, nil
	}
	event := trackEvent{
		Site: key, Name: argString(args, "event"), ClientID: argString(args, "client_id"),
		UserID: argString(args, "user_id"), SessionID: argString(args, "session_id"),
		InsertID: argString(args, "insert_id"), URL: argString(args, "url"),
		Referrer: argString(args, "referrer"), SampleRate: 1,
	}
	if event.Name == "" {
		return nil, errors.New("event (or events) is required")
	}
	if event.ClientID == "" {
		event.ClientID = "twohelixes-mcp"
	}
	if props, ok := args["props"].(map[string]any); ok {
		event.Properties = props
	}
	if ts := argString(args, "timestamp"); ts != "" {
		parsed, err := parseEventTime(ts)
		if err != nil {
			return nil, fmt.Errorf("timestamp: %w", err)
		}
		event.Time = &parsed
	}
	path, body, err := protocolTrackRequest("native", event)
	if err != nil {
		return nil, err
	}
	if err := s.app.client.Do(ctx, http.MethodPost, path, body, nil); err != nil {
		return nil, err
	}
	return map[string]any{"accepted": 1, "event": event.Name}, nil
}

func siteQuery(args map[string]any) (url.Values, error) {
	site := argString(args, "site_id")
	if site == "" {
		return nil, errors.New("site_id is required; call list_sites")
	}
	return url.Values{"site_id": {site}, "days": {strconv.Itoa(argInt(args, "days", 30))}}, nil
}

func copyArgs(q url.Values, args map[string]any, keys ...string) {
	for _, key := range keys {
		if value := argString(args, key); value != "" {
			q.Set(key, value)
		}
	}
}

func stepsArg(value any) (string, error) {
	switch v := value.(type) {
	case string:
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v), nil
		}
	case []any:
		parts := make([]string, 0, len(v))
		for _, item := range v {
			if text := strings.TrimSpace(fmt.Sprint(item)); text != "" {
				parts = append(parts, text)
			}
		}
		if len(parts) > 0 {
			return strings.Join(parts, ","), nil
		}
	}
	return "", errors.New("steps is required, e.g. page_view,sign_up_started,sign_up_completed")
}

func argString(args map[string]any, key string) string {
	switch v := args[key].(type) {
	case nil:
		return ""
	case string:
		return strings.TrimSpace(v)
	case float64:
		return strconv.FormatFloat(v, 'f', -1, 64)
	default:
		return strings.TrimSpace(fmt.Sprint(v))
	}
}

func argInt(args map[string]any, key string, fallback int) int {
	switch v := args[key].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case string:
		if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
			return n
		}
	}
	return fallback
}
