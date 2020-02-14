// Copyright (C) The Arvados Authors. All rights reserved.
//
// SPDX-License-Identifier: AGPL-3.0

package boot

import (
	"bytes"
	"context"
	"fmt"
	"path/filepath"

	"git.arvados.org/arvados.git/lib/cmd"
	"git.arvados.org/arvados.git/sdk/go/arvados"
)

type runServiceCommand struct {
	name    string
	command cmd.Handler
	depends []bootTask
}

func (runner runServiceCommand) String() string {
	return runner.name
}

func (runner runServiceCommand) Run(ctx context.Context, fail func(error), boot *Booter) error {
	boot.wait(ctx, runner.depends...)
	go func() {
		// runner.command.RunCommand() doesn't have access to
		// ctx, so it can't shut down by itself when the
		// caller cancels. We just abandon it.
		exitcode := runner.command.RunCommand(runner.name, []string{"-config", boot.configfile}, bytes.NewBuffer(nil), boot.Stderr, boot.Stderr)
		fail(fmt.Errorf("exit code %d", exitcode))
	}()
	return nil
}

type runGoProgram struct {
	src     string
	svc     arvados.Service
	depends []bootTask
}

func (runner runGoProgram) String() string {
	_, basename := filepath.Split(runner.src)
	return basename
}

func (runner runGoProgram) Run(ctx context.Context, fail func(error), boot *Booter) error {
	boot.wait(ctx, runner.depends...)
	boot.RunProgram(ctx, runner.src, nil, nil, "go", "install")
	if ctx.Err() != nil {
		return ctx.Err()
	}
	_, basename := filepath.Split(runner.src)
	if len(runner.svc.InternalURLs) > 0 {
		// Run one for each URL
		for u := range runner.svc.InternalURLs {
			u := u
			go func() {
				fail(boot.RunProgram(ctx, boot.tempdir, nil, []string{"ARVADOS_SERVICE_INTERNAL_URL=" + u.String()}, basename))
			}()
		}
	} else {
		// Just run one
		go func() {
			fail(boot.RunProgram(ctx, boot.tempdir, nil, nil, basename))
		}()
	}
	return nil
}
