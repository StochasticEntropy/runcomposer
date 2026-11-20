// The last line: a bug in one view shows a message and a way out instead of
// a blank page. The texts are passed in — when rendering itself has failed,
// this component must not depend on anything else that might have.

import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  title: string;
  hint: string;
  reloadLabel: string;
  /** Changing this (the route) gives the subtree another chance. */
  resetKey: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
  resetKey: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, resetKey: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey === state.resetKey ? null : { error: null, resetKey: props.resetKey };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("runcomposer UI error", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="state state-error" role="alert">
        <p className="state-title">{this.props.title}</p>
        <p>{this.props.hint}</p>
        <pre className="message-full">{String(this.state.error.message || this.state.error)}</pre>
        <button type="button" className="btn" onClick={() => window.location.reload()}>
          {this.props.reloadLabel}
        </button>
      </div>
    );
  }
}
