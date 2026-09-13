package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestChartCallsQueryAndRendersResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/query" || r.Method != http.MethodPost {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer test-key" {
			t.Fatalf("authorization = %q", got)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["q"] != "revenue by region" {
			t.Fatalf("question = %#v", body["q"])
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"chart_id": "chart-1",
			"figure": map[string]any{
				"data": []any{map[string]any{
					"type": "bar",
					"x":    []any{"North", "South"},
					"y":    []any{10, 7},
				}},
				"layout": map[string]any{"title": "Revenue"},
			},
		})
	}))
	defer server.Close()

	var out, errOut bytes.Buffer
	err := run(
		[]string{"--api-url", server.URL, "--api-key", "test-key", "chart", "revenue", "by", "region"},
		strings.NewReader(""), &out, &errOut,
	)
	if err != nil {
		t.Fatalf("run: %v (%s)", err, errOut.String())
	}
	for _, wanted := range []string{"Revenue", "North", "chart_id: chart-1"} {
		if !strings.Contains(out.String(), wanted) {
			t.Fatalf("output does not contain %q:\n%s", wanted, out.String())
		}
	}
}

func TestTrackProtocolsUseCompatibleEndpointsAndShapes(t *testing.T) {
	eventTime := time.Date(2026, time.August, 30, 1, 2, 3, 456_000_000, time.UTC)
	event := trackEvent{
		Site: "thw_test", Name: "checkout_completed", ClientID: "device-1",
		UserID: "user-7", SessionID: "1756512000000", InsertID: "event-9",
		URL: "https://shop.test/thanks", Referrer: "https://shop.test/cart",
		SampleRate: 0.5, Time: &eventTime, Properties: map[string]any{"amount": 42},
	}
	tests := []struct {
		protocol string
		path     string
		check    func(t *testing.T, body any)
	}{
		{
			protocol: "native", path: "/v1/collect",
			check: func(t *testing.T, raw any) {
				body := raw.(map[string]any)
				if body["event"] != event.Name || body["event_id"] != event.InsertID || body["session_id"] != event.SessionID {
					t.Fatalf("native body = %#v", body)
				}
			},
		},
		{
			protocol: "segment", path: "/v1/track",
			check: func(t *testing.T, raw any) {
				body := raw.(map[string]any)
				context, _ := body["context"].(map[string]any)
				if body["writeKey"] != event.Site || body["messageId"] != event.InsertID || context["sessionId"] != event.SessionID {
					t.Fatalf("Segment body = %#v", body)
				}
			},
		},
		{
			protocol: "ga4", path: "/mp/collect?measurement_id=thw_test",
			check: func(t *testing.T, raw any) {
				body := raw.(map[string]any)
				events, _ := body["events"].([]any)
				item, _ := events[0].(map[string]any)
				params, _ := item["params"].(map[string]any)
				if body["client_id"] != event.ClientID || item["name"] != event.Name || params["event_id"] != event.InsertID {
					t.Fatalf("GA4 body = %#v", body)
				}
			},
		},
		{
			protocol: "mixpanel", path: "/track",
			check: func(t *testing.T, raw any) {
				batch := raw.([]any)
				body := batch[0].(map[string]any)
				props, _ := body["properties"].(map[string]any)
				if body["event"] != event.Name || props["token"] != event.Site || props["$insert_id"] != event.InsertID || props["distinct_id"] != event.UserID || props["$device_id"] != event.ClientID {
					t.Fatalf("Mixpanel body = %#v", body)
				}
			},
		},
		{
			protocol: "amplitude", path: "/2/httpapi",
			check: func(t *testing.T, raw any) {
				body := raw.(map[string]any)
				events, _ := body["events"].([]any)
				item, _ := events[0].(map[string]any)
				if body["api_key"] != event.Site || item["event_type"] != event.Name || item["insert_id"] != event.InsertID {
					t.Fatalf("Amplitude body = %#v", body)
				}
				if item["session_id"] != int64(1756512000000) || item["time"] != eventTime.UnixMilli() {
					t.Fatalf("Amplitude times = %#v", item)
				}
			},
		},
	}
	for _, test := range tests {
		t.Run(test.protocol, func(t *testing.T) {
			path, body, err := protocolTrackRequest(test.protocol, event)
			if err != nil {
				t.Fatal(err)
			}
			if path != test.path {
				t.Fatalf("path = %q, want %q", path, test.path)
			}
			test.check(t, body)
		})
	}
}

func TestTrackCommandAcceptsPositionalCustomEventAndProps(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/track" {
			t.Fatalf("path = %q", r.URL.Path)
		}
		var batch []map[string]any
		if err := json.NewDecoder(r.Body).Decode(&batch); err != nil {
			t.Fatal(err)
		}
		if len(batch) != 1 {
			t.Fatalf("batch = %#v", batch)
		}
		body := batch[0]
		props, _ := body["properties"].(map[string]any)
		if body["event"] != "experiment_exposed" || props["variant"] != "blue" {
			t.Fatalf("body = %#v", body)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	var out bytes.Buffer
	err := run([]string{
		"--api-url", server.URL, "event", "--protocol", "mixpanel", "--site", "thw_test",
		"--props", `{"variant":"blue"}`, "experiment_exposed",
	}, strings.NewReader(""), &out, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	if out.String() != "accepted (mixpanel)\n" {
		t.Fatalf("output = %q", out.String())
	}
}

func TestTrackRejectsInvalidSamplingAndAmplitudeSession(t *testing.T) {
	event := trackEvent{Site: "thw_test", Name: "clicked", ClientID: "c", SessionID: "uuid", SampleRate: 1, Properties: map[string]any{}}
	if _, _, err := protocolTrackRequest("amplitude", event); err == nil || !strings.Contains(err.Error(), "integer") {
		t.Fatalf("Amplitude error = %v", err)
	}

	err := run([]string{"track", "--site", "thw_test", "--event", "clicked", "--sample-rate", "0"}, strings.NewReader(""), &bytes.Buffer{}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "sample-rate") {
		t.Fatalf("sample rate error = %v", err)
	}
}

func TestParseEventTime(t *testing.T) {
	for _, value := range []string{"2026-08-30T01:02:03Z", "1788051723", "1788051723000", "1788051723000000"} {
		parsed, err := parseEventTime(value)
		if err != nil {
			t.Fatalf("parse %q: %v", value, err)
		}
		if parsed.Unix() != 1788051723 {
			t.Fatalf("parse %q = %d", value, parsed.Unix())
		}
	}
}

func TestAnalyticsSchemaDisplaysCustomEventCatalog(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/analytics/schema" {
			t.Fatalf("request = %s %s", r.Method, r.URL.Path)
		}
		if r.URL.Query().Get("site_id") != "shop.test" || r.URL.Query().Get("days") != "14" || r.URL.Query().Get("event") != "purchase" {
			t.Fatalf("query = %s", r.URL.RawQuery)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"site_id": "shop.test",
			"window":  map[string]any{"start": 100, "end": 200},
			"events": []any{map[string]any{
				"name": "purchase", "observed_events": 8, "estimated_events": 10,
				"users":   6,
				"sources": []any{map[string]any{"name": "mixpanel", "observed_events": 5}},
				"properties": []any{
					map[string]any{"name": "amount", "types": []any{"number"}, "observed_events": 8},
					map[string]any{"name": "currency", "types": []any{"string"}, "observed_events": 8},
				},
			}},
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	err := run([]string{
		"--api-url", server.URL, "analytics", "schema", "--site", "shop.test",
		"--days", "14", "--event", "purchase",
	}, strings.NewReader(""), &out, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	for _, wanted := range []string{"site: shop.test", "purchase", "mixpanel:5", "amount", "number", "currency"} {
		if !strings.Contains(out.String(), wanted) {
			t.Fatalf("output does not contain %q:\n%s", wanted, out.String())
		}
	}
}

func TestAnalyticsSchemaJSONPreservesResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"site_id": "shop.test", "truncated": true,
			"events": []any{map[string]any{"name": "custom_event", "properties": []any{}}},
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	err := run([]string{
		"--api-url", server.URL, "analytics", "schema", "--site", "shop.test", "--json",
	}, strings.NewReader(""), &out, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	var response map[string]any
	if err := json.Unmarshal(out.Bytes(), &response); err != nil {
		t.Fatalf("JSON output: %v\n%s", err, out.String())
	}
	if response["site_id"] != "shop.test" || response["truncated"] != true {
		t.Fatalf("response = %#v", response)
	}
}

func TestAnalyticsPathsJoinsNodesAndSendsFilters(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/analytics/paths" {
			t.Fatalf("request = %s %s", r.Method, r.URL.Path)
		}
		query := r.URL.Query()
		for key, wanted := range map[string]string{
			"site_id": "shop.test", "days": "21", "start": "page_view:/pricing",
			"mode": "both", "depth": "6", "limit": "12",
		} {
			if query.Get(key) != wanted {
				t.Fatalf("query %s = %q, want %q (%s)", key, query.Get(key), wanted, r.URL.RawQuery)
			}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"truncated": true,
			"paths": []any{map[string]any{
				"path":     []any{"page_view:/pricing", "checkout_started", "purchase"},
				"sessions": 7, "estimated_sessions": 9, "users": 6, "rate": 0.35,
			}},
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	err := run([]string{
		"--api-url", server.URL, "analytics", "paths", "--site", "shop.test",
		"--days", "21", "--start", "page_view:/pricing", "--mode", "both",
		"--depth", "6", "--limit", "12",
	}, strings.NewReader(""), &out, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	for _, wanted := range []string{
		"warning: path input is truncated", "page_view:/pricing → checkout_start", "estimated_sessions",
	} {
		if !strings.Contains(out.String(), wanted) {
			t.Fatalf("output does not contain %q:\n%s", wanted, out.String())
		}
	}
}

func TestAnalyticsPathsJSONPreservesNodeArray(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"paths": []any{map[string]any{"path": []any{"landing", "signup"}, "sessions": 2}},
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	err := run([]string{
		"--api-url", server.URL, "analytics", "paths", "--site", "shop.test", "--json",
	}, strings.NewReader(""), &out, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	var response map[string]any
	if err := json.Unmarshal(out.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	paths := response["paths"].([]any)
	path := paths[0].(map[string]any)["path"].([]any)
	if len(path) != 2 || path[0] != "landing" || path[1] != "signup" {
		t.Fatalf("path = %#v", path)
	}
}

func TestAnalyticsRevenueKeepsCurrenciesSeparate(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/analytics/revenue" {
			t.Fatalf("request = %s %s", r.Method, r.URL.Path)
		}
		if r.URL.Query().Get("site_id") != "shop.test" || r.URL.Query().Get("days") != "30" {
			t.Fatalf("query = %s", r.URL.RawQuery)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"currencies": []any{
				map[string]any{"currency": "NZD", "revenue": 120.5},
				map[string]any{"currency": "USD", "revenue": 40},
			},
			"breakdown": []any{
				map[string]any{"currency": "NZD", "event": "purchase", "revenue": 130.5, "observed_transactions": 4, "users": 3},
				map[string]any{"currency": "NZD", "event": "refund", "revenue": -10, "observed_transactions": 1, "users": 1},
				map[string]any{"currency": "USD", "event": "purchase", "revenue": 40, "observed_transactions": 2, "users": 2},
			},
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	err := run([]string{
		"--api-url", server.URL, "analytics", "revenue", "--site", "shop.test", "--days", "30",
	}, strings.NewReader(""), &out, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	for _, wanted := range []string{"currencies:", "NZD", "120.5", "USD", "40", "breakdown:", "refund", "-10"} {
		if !strings.Contains(out.String(), wanted) {
			t.Fatalf("output does not contain %q:\n%s", wanted, out.String())
		}
	}
}

func TestAnalyticsPathsRejectsInvalidBounds(t *testing.T) {
	for _, args := range [][]string{
		{"analytics", "paths", "--site", "shop.test", "--mode", "visits"},
		{"analytics", "paths", "--site", "shop.test", "--depth", "1"},
		{"analytics", "paths", "--site", "shop.test", "--limit", "101"},
	} {
		err := run(args, strings.NewReader(""), &bytes.Buffer{}, &bytes.Buffer{})
		if err == nil {
			t.Fatalf("args %v unexpectedly succeeded", args)
		}
	}
}

func TestAnalyticsAskTargetsOwnedAnalyticsSource(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["analytics_site_id"] != "netwrck.com" || body["q"] != "signups by page" {
			t.Fatalf("body = %#v", body)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"figure": map[string]any{
				"data": []any{map[string]any{"type": "bar", "x": []any{"/", "/app"}, "y": []any{5, 3}}},
			},
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	err := run(
		[]string{"--api-url", server.URL, "analytics", "ask", "--site", "netwrck.com", "signups", "by", "page"},
		strings.NewReader(""), &out, &bytes.Buffer{},
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "/app") {
		t.Fatalf("output = %s", out.String())
	}
}
