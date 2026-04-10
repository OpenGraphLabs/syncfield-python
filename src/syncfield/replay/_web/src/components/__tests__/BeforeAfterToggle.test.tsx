import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import BeforeAfterToggle from "../BeforeAfterToggle";

describe("BeforeAfterToggle", () => {
  it("highlights the active mode", () => {
    render(
      <BeforeAfterToggle mode="after" disabled={false} onChange={() => {}} />,
    );
    const after = screen.getByRole("button", { name: /after/i });
    expect(after.className).toMatch(/bg-zinc-900/);
  });

  it("calls onChange when the inactive button is clicked", () => {
    const onChange = vi.fn();
    render(
      <BeforeAfterToggle mode="after" disabled={false} onChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /before/i }));
    expect(onChange).toHaveBeenCalledWith("before");
  });

  it("disables both buttons when disabled prop is true", () => {
    render(
      <BeforeAfterToggle mode="before" disabled onChange={() => {}} />,
    );
    expect(screen.getByRole("button", { name: /before/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /after/i })).toBeDisabled();
  });
});
