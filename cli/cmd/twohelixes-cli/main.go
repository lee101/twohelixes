package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/lee101/twohelixes-cli/internal/client"
	"github.com/lee101/twohelixes-cli/internal/termplot"
)

const version = "0.2.0"

type app struct {
	client *client.Client
	in     io.Reader
	out    io.Writer
	err    io.Writer
}

func main() {
	if err := run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run(args []string, in io.Reader, out, errOut io.Writer) error {
	global := flag.NewFlagSet("twohelixes-cli", flag.ContinueOnError)
	global.SetOutput(errOut)
	baseURL := global.String("api-url", env("TWOHELIXES_URL", "https://twohelixes.com"), "twoHelixes API base URL")
	apiKey := global.String("api-key", os.Getenv("TWOHELIXES_API_KEY"), "API key (or TWOHELIXES_API_KEY)")
	if err := global.Parse(args); err != nil {
		return err
	}
	args = global.Args()
	if len(args) == 0 {
		usage(out)
		return nil
	}
	application := &app{client: client.New(*baseURL, *apiKey), in: in, out: out, err: errOut}
	switch args[0] {
	case "chart", "graph":
		return application.chart(args[1:])
	case "render":
		return application.render(args[1:])
	case "analytics":
		return application.analytics(args[1:])
	case "track", "event":
		return application.track(args[1:])
	case "version", "--version", "-v":
		fmt.Fprintln(out, version)
		return nil
	case "help", "--help", "-h":
		usage(out)
		return nil
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func usage(out io.Writer) {
	fmt.Fprintln(out, `twohelixes-cli — generate, render, and query charts from the terminal

Usage:
  twohelixes-cli [--api-url URL] [--api-key KEY] chart [flags] QUESTION
  twohelixes-cli render [--width N] [--height N] [FILE|-]
  twohelixes-cli analytics sites|summary|events|schema|paths|revenue|funnel|ask|export [flags]
  twohelixes-cli track --site WRITE_KEY --event NAME [--protocol native|segment|ga4|mixpanel|amplitude]

Environment:
  TWOHELIXES_API_KEY   API key from the twoHelixes account page
  TWOHELIXES_URL       API base URL (default https://twohelixes.com)`)
}

func (a *app) chart(args []string) error {
	flags := flag.NewFlagSet("chart", flag.ContinueOnError)
	flags.SetOutput(a.err)
	question := flags.String("question", "", "question to ask")
	source := flags.String("source", "", "connected source ID")
	sample := flags.String("sample", "", "sample dataset name")
	datasets := flags.String("datasets", "", "comma-separated dataset IDs")
	dataFile := flags.String("data", "", "JSON array file to chart")
	mode := flags.String("mode", "light", "pipeline mode")
	jsonOut := flags.Bool("json", false, "print the complete JSON response")
	width := flags.Int("width", terminalWidth(), "terminal chart width")
	height := flags.Int("height", 18, "terminal chart height")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *question == "" {
		*question = strings.Join(flags.Args(), " ")
	}
	if strings.TrimSpace(*question) == "" {
		return errors.New("chart needs a question")
	}
	body := map[string]any{"q": *question, "mode": *mode}
	if *source != "" {
		body["source_id"] = *source
	}
	if *sample != "" {
		body["sample"] = *sample
	}
	if *datasets != "" {
		body["dataset_ids"] = splitList(*datasets)
	}
	if *dataFile != "" {
		var rows []map[string]any
		if err := decodeFile(*dataFile, a.in, &rows); err != nil {
			return fmt.Errorf("read chart data: %w", err)
		}
		body["data"] = rows
	}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodPost, "/v1/query", body, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}
	if interpretation, _ := response["interpretation"].(string); interpretation != "" {
		fmt.Fprintln(a.out, interpretation)
	}
	plot, err := termplot.Render(response, termplot.Options{Width: *width, Height: *height})
	if err != nil {
		return err
	}
	fmt.Fprint(a.out, plot)
	if id, _ := response["chart_id"].(string); id != "" {
		fmt.Fprintln(a.out, "chart_id:", id)
	}
	return nil
}

func (a *app) render(args []string) error {
	flags := flag.NewFlagSet("render", flag.ContinueOnError)
	flags.SetOutput(a.err)
	width := flags.Int("width", terminalWidth(), "terminal chart width")
	height := flags.Int("height", 18, "terminal chart height")
	if err := flags.Parse(args); err != nil {
		return err
	}
	path := "-"
	if flags.NArg() > 0 {
		path = flags.Arg(0)
	}
	var document any
	if err := decodeFile(path, a.in, &document); err != nil {
		return err
	}
	plot, err := termplot.Render(document, termplot.Options{Width: *width, Height: *height})
	if err != nil {
		return err
	}
	fmt.Fprint(a.out, plot)
	return nil
}

func (a *app) analytics(args []string) error {
	if len(args) == 0 {
		return errors.New("analytics needs sites, summary, events, schema, paths, revenue, funnel, ask, or export")
	}
	switch args[0] {
	case "sites":
		var response map[string]any
		if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/sites", nil, &response); err != nil {
			return err
		}
		return printRows(a.out, objectRows(response["sites"]), []string{"id", "domain", "name"})
	case "summary":
		return a.analyticsSummary(args[1:])
	case "events":
		return a.analyticsEvents(args[1:])
	case "schema", "catalog":
		return a.analyticsSchema(args[1:])
	case "paths", "journeys":
		return a.analyticsPaths(args[1:])
	case "revenue":
		return a.analyticsRevenue(args[1:])
	case "funnel", "flow":
		return a.analyticsFunnel(args[1:])
	case "ask", "query":
		return a.analyticsAsk(args[1:])
	case "export":
		return a.analyticsExport(args[1:])
	default:
		return fmt.Errorf("unknown analytics command %q", args[0])
	}
}

func (a *app) analyticsSummary(args []string) error {
	flags, site, days, jsonOut := analyticsFlags("summary", a.err)
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *site == "" {
		return errors.New("--site is required")
	}
	query := url.Values{"site_id": {*site}, "days": {strconv.Itoa(*days)}}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/summary"+client.Query(query), nil, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}
	totals, _ := response["totals"].(map[string]any)
	for _, key := range []string{"events", "observed_events", "users", "sessions", "page_views", "engaged_sessions", "bounce_rate"} {
		if value, ok := totals[key]; ok {
			fmt.Fprintf(a.out, "%-18s %v\n", key, value)
		}
	}
	for _, group := range []string{"top_events", "top_pages", "referrers"} {
		rows := objectRows(response[group])
		if len(rows) == 0 {
			continue
		}
		fmt.Fprintln(a.out, "\n"+strings.ReplaceAll(group, "_", " ")+":")
		_ = printRows(a.out, rows, []string{"key", "events", "users"})
	}
	return nil
}

func (a *app) analyticsEvents(args []string) error {
	flags, site, days, jsonOut := analyticsFlags("events", a.err)
	limit := flags.Int("limit", 100, "maximum events")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *site == "" {
		return errors.New("--site is required")
	}
	query := url.Values{"site_id": {*site}, "days": {strconv.Itoa(*days)}, "limit": {strconv.Itoa(*limit)}}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/events"+client.Query(query), nil, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}
	return printRows(a.out, objectRows(response["events"]), []string{"ts", "event_name", "page_path", "device", "user_id", "sample_weight"})
}

func (a *app) analyticsSchema(args []string) error {
	flags, site, days, jsonOut := analyticsFlags("schema", a.err)
	event := flags.String("event", "", "only describe one event name")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *site == "" {
		return errors.New("--site is required")
	}
	query := url.Values{"site_id": {*site}, "days": {strconv.Itoa(*days)}}
	if *event != "" {
		query.Set("event", *event)
	}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/schema"+client.Query(query), nil, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}

	events := objectRows(response["events"])
	if siteID := cell(response["site_id"]); siteID != "" {
		fmt.Fprintln(a.out, "site:", siteID)
	}
	if window, ok := response["window"].(map[string]any); ok {
		fmt.Fprintf(a.out, "window: %v to %v\n", window["start"], window["end"])
	}
	if truncated, _ := response["truncated"].(bool); truncated {
		fmt.Fprintln(a.out, "warning: property catalog is truncated")
	}
	if len(events) == 0 {
		fmt.Fprintln(a.out, "No events")
		return nil
	}

	summary := make([]map[string]any, 0, len(events))
	for _, eventRow := range events {
		row := make(map[string]any, len(eventRow)+1)
		for key, value := range eventRow {
			row[key] = value
		}
		var sources []string
		for _, source := range objectRows(eventRow["sources"]) {
			label := cell(source["name"])
			if count := cell(source["observed_events"]); count != "" {
				label += ":" + count
			}
			if label != "" {
				sources = append(sources, label)
			}
		}
		row["sources"] = strings.Join(sources, ", ")
		summary = append(summary, row)
	}
	fmt.Fprintln(a.out, "\nevents:")
	_ = printRows(a.out, summary, []string{
		"name", "observed_events", "estimated_events", "users", "sources",
	})

	for _, eventRow := range events {
		properties := objectRows(eventRow["properties"])
		if len(properties) == 0 {
			continue
		}
		fmt.Fprintf(a.out, "\n%s properties:\n", cell(eventRow["name"]))
		_ = printRows(a.out, properties, []string{"name", "types", "observed_events"})
	}
	return nil
}

func (a *app) analyticsPaths(args []string) error {
	flags, site, days, jsonOut := analyticsFlags("paths", a.err)
	start := flags.String("start", "", "only paths beginning at this event or page node")
	mode := flags.String("mode", "both", "path nodes: both, events, or pages")
	depth := flags.Int("depth", 5, "maximum nodes in each path")
	limit := flags.Int("limit", 20, "maximum paths")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *site == "" {
		return errors.New("--site is required")
	}
	*mode = strings.ToLower(strings.TrimSpace(*mode))
	if *mode != "both" && *mode != "events" && *mode != "pages" {
		return errors.New("--mode must be both, events, or pages")
	}
	if *depth < 2 || *depth > 12 {
		return errors.New("--depth must be between 2 and 12")
	}
	if *limit < 1 || *limit > 100 {
		return errors.New("--limit must be between 1 and 100")
	}
	query := url.Values{
		"site_id": {*site}, "days": {strconv.Itoa(*days)}, "mode": {*mode},
		"depth": {strconv.Itoa(*depth)}, "limit": {strconv.Itoa(*limit)},
	}
	if *start != "" {
		query.Set("start", *start)
	}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/paths"+client.Query(query), nil, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}
	if truncated, _ := response["truncated"].(bool); truncated {
		fmt.Fprintln(a.out, "warning: path input is truncated")
	}
	paths := objectRows(response["paths"])
	for _, row := range paths {
		nodes, _ := row["path"].([]any)
		labels := make([]string, 0, len(nodes))
		for _, node := range nodes {
			labels = append(labels, cell(node))
		}
		row["path"] = strings.Join(labels, " → ")
	}
	return printRows(a.out, paths, []string{
		"path", "sessions", "estimated_sessions", "users", "rate",
	})
}

func (a *app) analyticsRevenue(args []string) error {
	flags, site, days, jsonOut := analyticsFlags("revenue", a.err)
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *site == "" {
		return errors.New("--site is required")
	}
	query := url.Values{"site_id": {*site}, "days": {strconv.Itoa(*days)}}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/revenue"+client.Query(query), nil, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}
	if truncated, _ := response["truncated"].(bool); truncated {
		fmt.Fprintln(a.out, "warning: revenue input is truncated")
	}
	// Currency totals remain separate: summing unlike currencies would create
	// a plausible-looking but meaningless number without an FX policy.
	fmt.Fprintln(a.out, "currencies:")
	_ = printRows(a.out, objectRows(response["currencies"]), []string{"currency", "revenue"})
	fmt.Fprintln(a.out, "\nbreakdown:")
	return printRows(a.out, objectRows(response["breakdown"]), []string{
		"currency", "event", "revenue", "observed_transactions", "users",
	})
}

func (a *app) analyticsFunnel(args []string) error {
	flags, site, days, jsonOut := analyticsFlags("funnel", a.err)
	steps := flags.String("steps", "", "comma-separated ordered events")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *site == "" || *steps == "" {
		return errors.New("--site and --steps are required")
	}
	query := url.Values{"site_id": {*site}, "days": {strconv.Itoa(*days)}, "steps": {*steps}}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/funnel"+client.Query(query), nil, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}
	return printRows(a.out, objectRows(response["steps"]), []string{
		"event", "sessions", "estimated_sessions", "users", "step_rate", "estimated_step_rate", "dropoff",
	})
}

func (a *app) analyticsAsk(args []string) error {
	flags, site, days, jsonOut := analyticsFlags("ask", a.err)
	question := flags.String("question", "", "question to ask")
	width := flags.Int("width", terminalWidth(), "terminal chart width")
	height := flags.Int("height", 18, "terminal chart height")
	limit := flags.Int("limit", 50000, "maximum recent event rows")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *question == "" {
		*question = strings.Join(flags.Args(), " ")
	}
	if *site == "" || *question == "" {
		return errors.New("--site and a question are required")
	}
	body := map[string]any{
		"q": *question, "analytics_site_id": *site, "days": *days, "limit": *limit,
	}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodPost, "/v1/query", body, &response); err != nil {
		return err
	}
	if *jsonOut {
		return writeJSON(a.out, response)
	}
	plot, err := termplot.Render(response, termplot.Options{Width: *width, Height: *height})
	if err != nil {
		return err
	}
	fmt.Fprint(a.out, plot)
	return nil
}

func (a *app) analyticsExport(args []string) error {
	flags, site, days, _ := analyticsFlags("export", a.err)
	limit := flags.Int("limit", 1000, "maximum events")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *site == "" {
		return errors.New("--site is required")
	}
	query := url.Values{"site_id": {*site}, "days": {strconv.Itoa(*days)}, "limit": {strconv.Itoa(*limit)}}
	var response map[string]any
	if err := a.client.Do(context.Background(), http.MethodGet, "/v1/analytics/export"+client.Query(query), nil, &response); err != nil {
		return err
	}
	return writeJSON(a.out, response)
}

func (a *app) track(args []string) error {
	flags := flag.NewFlagSet("track", flag.ContinueOnError)
	flags.SetOutput(a.err)
	site := flags.String("site", os.Getenv("TWOHELIXES_WRITE_KEY"), "analytics site write key")
	event := flags.String("event", "", "event name")
	clientID := flags.String("client", "twohelixes-cli", "anonymous client ID")
	userID := flags.String("user", "", "known user ID")
	properties := flags.String("props", "{}", "event properties as JSON, or @FILE")
	protocol := flags.String("protocol", "native", "native, segment, ga4, mixpanel, or amplitude")
	sessionID := flags.String("session", "", "session ID")
	timestamp := flags.String("timestamp", "", "event time as RFC3339 or Unix seconds/milliseconds/microseconds")
	insertID := flags.String("insert-id", "", "idempotency key supplied by the event producer")
	pageURL := flags.String("url", "", "current page URL")
	referrer := flags.String("referrer", "", "referring page URL")
	sampleRate := flags.Float64("sample-rate", 1, "client sample rate in (0,1]")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *event == "" && flags.NArg() > 0 {
		*event = flags.Arg(0)
	}
	if *site == "" || *event == "" {
		return errors.New("--site and --event are required")
	}
	if *sampleRate <= 0 || *sampleRate > 1 {
		return errors.New("--sample-rate must be greater than 0 and at most 1")
	}
	var props map[string]any
	if err := decodeJSONObject(*properties, a.in, &props); err != nil {
		return fmt.Errorf("parse --props: %w", err)
	}
	var eventTime *time.Time
	if *timestamp != "" {
		parsed, err := parseEventTime(*timestamp)
		if err != nil {
			return fmt.Errorf("parse --timestamp: %w", err)
		}
		eventTime = &parsed
	}
	request := trackEvent{
		Site: *site, Name: *event, ClientID: *clientID, UserID: *userID,
		SessionID: *sessionID, InsertID: *insertID, URL: *pageURL,
		Referrer: *referrer, SampleRate: *sampleRate, Time: eventTime,
		Properties: props,
	}
	path, body, err := protocolTrackRequest(*protocol, request)
	if err != nil {
		return err
	}
	if err := a.client.Do(context.Background(), http.MethodPost, path, body, nil); err != nil {
		return err
	}
	fmt.Fprintf(a.out, "accepted (%s)\n", strings.ToLower(*protocol))
	return nil
}

type trackEvent struct {
	Site       string
	Name       string
	ClientID   string
	UserID     string
	SessionID  string
	InsertID   string
	URL        string
	Referrer   string
	SampleRate float64
	Time       *time.Time
	Properties map[string]any
}

func protocolTrackRequest(protocol string, event trackEvent) (string, any, error) {
	protocol = strings.ToLower(strings.TrimSpace(protocol))
	props := make(map[string]any, len(event.Properties)+4)
	for key, value := range event.Properties {
		props[key] = value
	}
	switch protocol {
	case "native":
		body := map[string]any{
			"site_id": event.Site, "event": event.Name, "client_id": event.ClientID,
			"props": props, "sample_rate": event.SampleRate,
		}
		addCommonEventFields(body, event)
		return "/v1/collect", body, nil
	case "segment":
		body := map[string]any{
			"writeKey": event.Site, "type": "track", "event": event.Name,
			"anonymousId": event.ClientID, "properties": props,
			"sample_rate": event.SampleRate,
		}
		if event.UserID != "" {
			body["userId"] = event.UserID
		}
		if event.Time != nil {
			body["timestamp"] = event.Time.UTC().Format(time.RFC3339Nano)
		}
		if event.InsertID != "" {
			body["messageId"] = event.InsertID
		}
		context := map[string]any{}
		if event.SessionID != "" {
			context["sessionId"] = event.SessionID
			body["session_id"] = event.SessionID
		}
		if event.URL != "" || event.Referrer != "" {
			context["page"] = map[string]any{"url": event.URL, "referrer": event.Referrer}
		}
		if len(context) != 0 {
			body["context"] = context
		}
		return "/v1/track", body, nil
	case "ga4", "google", "google-analytics":
		params := props
		params["sample_rate"] = event.SampleRate
		if event.SessionID != "" {
			params["session_id"] = event.SessionID
		}
		if event.URL != "" {
			params["page_location"] = event.URL
		}
		if event.Referrer != "" {
			params["page_referrer"] = event.Referrer
		}
		gaEvent := map[string]any{"name": event.Name, "params": params}
		if event.InsertID != "" {
			params["event_id"] = event.InsertID
		}
		body := map[string]any{"client_id": event.ClientID, "events": []any{gaEvent}}
		if event.UserID != "" {
			body["user_id"] = event.UserID
		}
		if event.Time != nil {
			body["timestamp_micros"] = event.Time.UnixMicro()
		}
		return "/mp/collect" + client.Query(url.Values{"measurement_id": {event.Site}}), body, nil
	case "mixpanel":
		props["token"] = event.Site
		props["distinct_id"] = event.ClientID
		props["sample_rate"] = event.SampleRate
		if event.UserID != "" {
			props["$device_id"] = event.ClientID
			props["$user_id"] = event.UserID
			props["distinct_id"] = event.UserID
		}
		if event.InsertID != "" {
			props["$insert_id"] = event.InsertID
		}
		if event.SessionID != "" {
			props["session_id"] = event.SessionID
		}
		if event.URL != "" {
			props["$current_url"] = event.URL
		}
		if event.Referrer != "" {
			props["$referrer"] = event.Referrer
		}
		if event.Time != nil {
			props["time"] = event.Time.Unix()
		}
		return "/track", []any{map[string]any{"event": event.Name, "properties": props}}, nil
	case "amplitude":
		amplitudeEvent := map[string]any{
			"event_type": event.Name, "device_id": event.ClientID,
			"event_properties": props,
		}
		if event.UserID != "" {
			amplitudeEvent["user_id"] = event.UserID
		}
		if event.InsertID != "" {
			amplitudeEvent["insert_id"] = event.InsertID
		}
		if event.SessionID != "" {
			session, err := strconv.ParseInt(event.SessionID, 10, 64)
			if err != nil {
				return "", nil, errors.New("Amplitude --session must be an integer Unix-millisecond session ID")
			}
			amplitudeEvent["session_id"] = session
		}
		if event.Time != nil {
			amplitudeEvent["time"] = event.Time.UnixMilli()
		}
		if event.URL != "" {
			amplitudeEvent["event_properties"].(map[string]any)["page_location"] = event.URL
		}
		if event.Referrer != "" {
			amplitudeEvent["event_properties"].(map[string]any)["page_referrer"] = event.Referrer
		}
		amplitudeEvent["event_properties"].(map[string]any)["sample_rate"] = event.SampleRate
		return "/2/httpapi", map[string]any{"api_key": event.Site, "events": []any{amplitudeEvent}}, nil
	default:
		return "", nil, fmt.Errorf("unknown --protocol %q (want native, segment, ga4, mixpanel, or amplitude)", protocol)
	}
}

func addCommonEventFields(body map[string]any, event trackEvent) {
	if event.UserID != "" {
		body["user_id"] = event.UserID
	}
	if event.SessionID != "" {
		body["session_id"] = event.SessionID
	}
	if event.InsertID != "" {
		body["event_id"] = event.InsertID
	}
	if event.URL != "" {
		body["page_location"] = event.URL
	}
	if event.Referrer != "" {
		body["referrer"] = event.Referrer
	}
	if event.Time != nil {
		body["timestamp"] = event.Time.Unix()
	}
}

func parseEventTime(value string) (time.Time, error) {
	if parsed, err := time.Parse(time.RFC3339Nano, value); err == nil {
		return parsed, nil
	}
	numeric, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return time.Time{}, errors.New("use RFC3339 or a Unix timestamp")
	}
	switch {
	case numeric > 100_000_000_000_000:
		return time.Unix(0, numeric*1_000).UTC(), nil
	case numeric > 100_000_000_000:
		return time.UnixMilli(numeric).UTC(), nil
	default:
		return time.Unix(numeric, 0).UTC(), nil
	}
}

func decodeJSONObject(value string, in io.Reader, target *map[string]any) error {
	if strings.HasPrefix(value, "@") {
		path := strings.TrimPrefix(value, "@")
		if path == "" {
			return errors.New("@ must be followed by a JSON file path or -")
		}
		return decodeFile(path, in, target)
	}
	decoder := json.NewDecoder(strings.NewReader(value))
	decoder.UseNumber()
	return decoder.Decode(target)
}

func analyticsFlags(name string, output io.Writer) (*flag.FlagSet, *string, *int, *bool) {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(output)
	site := flags.String("site", "", "analytics site ID")
	days := flags.Int("days", 30, "reporting window in days")
	jsonOut := flags.Bool("json", false, "print JSON")
	return flags, site, days, jsonOut
}

func decodeFile(path string, in io.Reader, target any) error {
	reader := in
	if path != "-" {
		file, err := os.Open(path)
		if err != nil {
			return err
		}
		defer file.Close()
		reader = file
	}
	decoder := json.NewDecoder(io.LimitReader(reader, 32<<20))
	decoder.UseNumber()
	return decoder.Decode(target)
}

func writeJSON(out io.Writer, value any) error {
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

func objectRows(value any) []map[string]any {
	list, _ := value.([]any)
	rows := make([]map[string]any, 0, len(list))
	for _, item := range list {
		if row, ok := item.(map[string]any); ok {
			rows = append(rows, row)
		}
	}
	return rows
}

func printRows(out io.Writer, rows []map[string]any, columns []string) error {
	if len(rows) == 0 {
		fmt.Fprintln(out, "No rows")
		return nil
	}
	widths := make([]int, len(columns))
	for index, column := range columns {
		widths[index] = len(column)
	}
	for _, row := range rows {
		for index, column := range columns {
			widths[index] = min(max(widths[index], len(cell(row[column]))), 36)
		}
	}
	for index, column := range columns {
		fmt.Fprintf(out, "%-*s", widths[index]+2, column)
	}
	fmt.Fprintln(out)
	for index := range columns {
		fmt.Fprint(out, strings.Repeat("─", widths[index]), "  ")
	}
	fmt.Fprintln(out)
	for _, row := range rows {
		for index, column := range columns {
			fmt.Fprintf(out, "%-*s", widths[index]+2, truncate(cell(row[column]), widths[index]))
		}
		fmt.Fprintln(out)
	}
	return nil
}

func cell(value any) string {
	switch value := value.(type) {
	case nil:
		return ""
	case float64:
		return strconv.FormatFloat(value, 'f', -1, 64)
	case map[string]any, []any:
		encoded, _ := json.Marshal(value)
		return string(encoded)
	default:
		return fmt.Sprint(value)
	}
}

func truncate(value string, width int) string {
	runes := []rune(value)
	if len(runes) <= width {
		return value
	}
	if width < 2 {
		return string(runes[:width])
	}
	return string(runes[:width-1]) + "…"
}

func splitList(value string) []string {
	var out []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func terminalWidth() int {
	value, _ := strconv.Atoi(os.Getenv("COLUMNS"))
	if value < 40 {
		return 72
	}
	return value
}
