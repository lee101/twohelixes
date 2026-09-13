package termplot

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
)

type Options struct {
	Width  int
	Height int
}

func Render(document any, options Options) (string, error) {
	root, ok := document.(map[string]any)
	if !ok {
		return "", fmt.Errorf("chart JSON must be an object")
	}
	if figure, ok := root["figure"].(map[string]any); ok {
		root = figure
	}
	traces, ok := root["data"].([]any)
	if !ok || len(traces) == 0 {
		return "", fmt.Errorf("chart has no Plotly traces")
	}
	if options.Width <= 0 {
		options.Width = 72
	}
	if options.Height <= 0 {
		options.Height = 18
	}
	var out strings.Builder
	if title := chartTitle(root); title != "" {
		out.WriteString(title)
		out.WriteByte('\n')
	}
	first, _ := traces[0].(map[string]any)
	kind := stringValue(first["type"])
	if kind == "bar" || kind == "pie" || categorical(first["x"]) {
		out.WriteString(renderBars(traces, options.Width))
	} else {
		out.WriteString(renderCartesian(traces, options.Width, options.Height))
	}
	return strings.TrimRight(out.String(), "\n") + "\n", nil
}

func Decode(data []byte) (any, error) {
	var value any
	if err := json.Unmarshal(data, &value); err != nil {
		return nil, err
	}
	return value, nil
}

func chartTitle(figure map[string]any) string {
	layout, _ := figure["layout"].(map[string]any)
	switch title := layout["title"].(type) {
	case string:
		return title
	case map[string]any:
		return stringValue(title["text"])
	default:
		return ""
	}
}

func renderBars(traces []any, width int) string {
	type item struct {
		label string
		value float64
	}
	var items []item
	maxLabel := 0
	maxValue := 0.0
	for _, raw := range traces {
		trace, _ := raw.(map[string]any)
		labels := values(trace["x"])
		numericValues := numbers(trace["y"])
		if stringValue(trace["type"]) == "pie" {
			labels = values(trace["labels"])
			numericValues = numbers(trace["values"])
		}
		if stringValue(trace["orientation"]) == "h" {
			labels = values(trace["y"])
			numericValues = numbers(trace["x"])
		}
		name := stringValue(trace["name"])
		for i := 0; i < len(labels) && i < len(numericValues); i++ {
			label := labels[i]
			if name != "" && len(traces) > 1 {
				label = name + " / " + label
			}
			items = append(items, item{label: label, value: numericValues[i]})
			maxLabel = max(maxLabel, len([]rune(label)))
			maxValue = math.Max(maxValue, math.Abs(numericValues[i]))
		}
	}
	if len(items) == 0 {
		return "No numeric marks\n"
	}
	maxLabel = min(maxLabel, max(8, width/3))
	barWidth := max(4, width-maxLabel-15)
	var out strings.Builder
	for _, current := range items {
		label := truncate(current.label, maxLabel)
		marks := 0
		if maxValue > 0 {
			marks = int(math.Round(math.Abs(current.value) / maxValue * float64(barWidth)))
		}
		fmt.Fprintf(&out, "%*s │ %s %s\n", maxLabel, label, strings.Repeat("█", marks), formatNumber(current.value))
	}
	return out.String()
}

func renderCartesian(traces []any, width, height int) string {
	width = max(20, width-10)
	height = max(6, height)
	grid := make([][]rune, height)
	for y := range grid {
		grid[y] = []rune(strings.Repeat(" ", width))
	}
	type point struct{ x, y float64 }
	series := make([][]point, 0, len(traces))
	minX, maxX := math.Inf(1), math.Inf(-1)
	minY, maxY := math.Inf(1), math.Inf(-1)
	for _, raw := range traces {
		trace, _ := raw.(map[string]any)
		xs, ys := numbers(trace["x"]), numbers(trace["y"])
		points := make([]point, 0, min(len(xs), len(ys)))
		for i := 0; i < len(xs) && i < len(ys); i++ {
			if math.IsNaN(xs[i]) || math.IsNaN(ys[i]) {
				continue
			}
			points = append(points, point{xs[i], ys[i]})
			minX, maxX = math.Min(minX, xs[i]), math.Max(maxX, xs[i])
			minY, maxY = math.Min(minY, ys[i]), math.Max(maxY, ys[i])
		}
		series = append(series, points)
	}
	if math.IsInf(minX, 1) {
		return "No numeric marks\n"
	}
	if minX == maxX {
		maxX = minX + 1
	}
	if minY == maxY {
		maxY = minY + 1
	}
	marks := []rune{'●', '◆', '▲', '■', '✦', '○'}
	for index, points := range series {
		mark := marks[index%len(marks)]
		for _, current := range points {
			x := int(math.Round((current.x - minX) / (maxX - minX) * float64(width-1)))
			y := height - 1 - int(math.Round((current.y-minY)/(maxY-minY)*float64(height-1)))
			if grid[y][x] != ' ' {
				grid[y][x] = '✚'
			} else {
				grid[y][x] = mark
			}
		}
	}
	var out strings.Builder
	for row, line := range grid {
		label := ""
		if row == 0 {
			label = formatNumber(maxY)
		} else if row == height-1 {
			label = formatNumber(minY)
		}
		fmt.Fprintf(&out, "%8s │%s\n", label, string(line))
	}
	fmt.Fprintf(&out, "         └%s\n", strings.Repeat("─", width))
	fmt.Fprintf(&out, "          %-*s%s\n", max(1, width-len(formatNumber(maxX))), formatNumber(minX), formatNumber(maxX))
	return out.String()
}

func categorical(value any) bool {
	list, ok := value.([]any)
	if !ok || len(list) == 0 {
		return false
	}
	_, numeric := number(list[0])
	return !numeric
}

func values(value any) []string {
	list, _ := value.([]any)
	out := make([]string, 0, len(list))
	for _, item := range list {
		out = append(out, stringValue(item))
	}
	return out
}

func numbers(value any) []float64 {
	list, _ := value.([]any)
	out := make([]float64, 0, len(list))
	for index, item := range list {
		if n, ok := number(item); ok {
			out = append(out, n)
		} else {
			out = append(out, float64(index))
		}
	}
	return out
}

func number(value any) (float64, bool) {
	switch value := value.(type) {
	case float64:
		return value, true
	case json.Number:
		n, err := value.Float64()
		return n, err == nil
	case string:
		n, err := strconv.ParseFloat(value, 64)
		if err == nil {
			return n, true
		}
		if parsed, dateErr := time.Parse(time.RFC3339, value); dateErr == nil {
			return float64(parsed.Unix()), true
		}
		return 0, false
	default:
		return 0, false
	}
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprint(value)
}

func formatNumber(value float64) string {
	abs := math.Abs(value)
	if abs >= 1_000_000_000 {
		return fmt.Sprintf("%.1fB", value/1_000_000_000)
	}
	if abs >= 1_000_000 {
		return fmt.Sprintf("%.1fM", value/1_000_000)
	}
	if abs >= 1_000 {
		return fmt.Sprintf("%.1fk", value/1_000)
	}
	return strconv.FormatFloat(value, 'f', -1, 64)
}

func truncate(value string, width int) string {
	runes := []rune(value)
	if len(runes) <= width {
		return value
	}
	if width <= 1 {
		return string(runes[:width])
	}
	return string(runes[:width-1]) + "…"
}

func SortedKeys(value map[string]any) []string {
	keys := make([]string, 0, len(value))
	for key := range value {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
