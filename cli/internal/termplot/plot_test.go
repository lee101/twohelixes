package termplot

import (
	"strings"
	"testing"
)

func TestRenderBarFigure(t *testing.T) {
	document := map[string]any{
		"figure": map[string]any{
			"data": []any{map[string]any{
				"type": "bar",
				"x":    []any{"North", "South"},
				"y":    []any{12.0, 6.0},
			}},
			"layout": map[string]any{"title": map[string]any{"text": "Revenue by region"}},
		},
	}
	result, err := Render(document, Options{Width: 48, Height: 10})
	if err != nil {
		t.Fatal(err)
	}
	for _, wanted := range []string{"Revenue by region", "North", "South", "█", "12"} {
		if !strings.Contains(result, wanted) {
			t.Fatalf("rendered chart does not contain %q:\n%s", wanted, result)
		}
	}
}

func TestRenderRejectsEmptyFigure(t *testing.T) {
	_, err := Render(map[string]any{"data": []any{}}, Options{})
	if err == nil {
		t.Fatal("empty figure should fail")
	}
}
